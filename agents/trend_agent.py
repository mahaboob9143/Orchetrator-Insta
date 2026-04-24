"""
agents/trend_agent.py — TrendAgent for InstaAgent.

Responsibilities:
  1. Fetch trending Islamic keywords from Google Trends via Pytrends.
  2. Pull own account post insights from Meta Graph API to derive
     historically best posting hours.
  3. Optionally scrape 1-2 competitor Instagram pages (read-only via
     instagrapi) when flags.trend_scraping is enabled.
  4. Persist all trend data to the trends table.
  5. Return a TrendOutput with topics + best posting hours.
"""

import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import requests
from pytrends.request import TrendReq

from core.db import save_trend
from core.flags import get_config, get_flags
from core.logger import get_logger
from core.retry import retry

logger = get_logger("TrendAgent")


# ─── Output dataclass ─────────────────────────────────────────────────────────

@dataclass
class TrendOutput:
    topics: List[str] = field(default_factory=list)
    best_hours: List[int] = field(default_factory=list)
    patterns: Dict = field(default_factory=dict)


# ─── Agent ────────────────────────────────────────────────────────────────────

class TrendAgent:
    """
    Analyzes Islamic content trends to drive the rest of the pipeline.
    """

    # Fallback default hours if no insights are available yet
    _DEFAULT_HOURS = [8, 14, 21]

    def __init__(self):
        self.config = get_config()
        self.flags = get_flags()

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self) -> TrendOutput:
        """
        Execute the full trend analysis cycle.

        Returns:
            TrendOutput with deduplicated topics (up to 10) and best hours.
        """
        # Hot-reload config and flags
        self.config = get_config()
        self.flags = get_flags()

        logger.info("TrendAgent: starting trend analysis...")
        topics: List[str] = []

        # 1. Google Trends (primary)
        try:
            pytrend_topics = self._fetch_pytrends()
            topics.extend(pytrend_topics)
            logger.info(f"Pytrends returned {len(pytrend_topics)} topics")
        except Exception as exc:
            logger.error(f"Pytrends fetch failed: {exc}")

        # 2. Own account insights → best posting hours
        try:
            best_hours = self._fetch_own_insights()
        except Exception as exc:
            logger.warning(f"Own account insights failed (using defaults): {exc}")
            best_hours = self._DEFAULT_HOURS

        # 3. Competitor scraping (only when flag is on)
        if self.flags.trend_scraping:
            try:
                competitor_topics = self._scrape_competitors()
                topics.extend(competitor_topics)
                logger.info(f"Competitor scraping added {len(competitor_topics)} topics")
            except Exception as exc:
                logger.warning(f"Competitor scraping failed (non-critical): {exc}")

        # 4. Fallback to config keywords if nothing came back
        if not topics:
            topics = self.config.get("pytrends", {}).get("keywords", [
                "Islamic quotes", "Quran", "Islamic reminder"
            ])
            logger.warning("No trend data fetched — using config keyword fallback")

        # Deduplicate preserving order, cap at 10
        seen: Dict[str, None] = {}
        for t in topics:
            seen[t] = None
        final_topics = list(seen.keys())[:10]

        logger.info(
            f"TrendAgent complete. Topics: {final_topics[:5]}... "
            f"Best hours: {best_hours}"
        )
        return TrendOutput(topics=final_topics, best_hours=best_hours)

    # ── Pytrends ──────────────────────────────────────────────────────────────

    @retry(max_attempts=3, backoff_factor=2, initial_wait=5.0)
    def _fetch_pytrends(self) -> List[str]:
        """
        Fetch keyword interest scores from Google Trends.
        Processes keywords in batches of 5 (Pytrends limit per request).
        """
        cfg = self.config.get("pytrends", {})
        keywords: List[str] = cfg.get("keywords", ["Islamic quotes", "Quran"])
        timeframe: str = cfg.get("timeframe", "now 7-d")
        geo: str = cfg.get("geo", "")

        pytrends = TrendReq(
            hl="en-US",
            tz=330,
            timeout=(10, 25),
            retries=2,
            backoff_factor=1.5,
        )

        scores: Dict[str, float] = {}

        for i in range(0, len(keywords), 5):
            batch = keywords[i : i + 5]
            pytrends.build_payload(batch, timeframe=timeframe, geo=geo)

            df = pytrends.interest_over_time()
            if df.empty:
                time.sleep(random.uniform(3, 6))
                continue

            for kw in batch:
                if kw in df.columns:
                    score = float(df[kw].mean())
                    scores[kw] = score
                    save_trend(topic=kw, score=score, source="pytrends")

            # Human-like delay between batches to avoid 429
            time.sleep(random.uniform(3, 7))

        sorted_topics = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [topic for topic, _ in sorted_topics]

    # ── Own account insights ──────────────────────────────────────────────────

    def _fetch_own_insights(self) -> List[int]:
        """
        Pull recent post metrics from own IG account via Graph API.
        Computes average reach per hour to determine best posting times.

        Returns:
            List of up to 3 hours (0-23) with highest average reach.
        """
        token = os.getenv("META_ACCESS_TOKEN")
        account_id = os.getenv("IG_ACCOUNT_ID")

        if not token or not account_id:
            logger.warning("META credentials missing — skipping own insights")
            return self._DEFAULT_HOURS

        url = f"https://graph.facebook.com/v19.0/{account_id}/media"
        params = {
            "fields": "like_count,comments_count,timestamp,reach",
            "limit": 25,
            "access_token": token,
        }

        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        posts = resp.json().get("data", [])

        if not posts:
            logger.info("No own posts found — using default hours")
            return self._DEFAULT_HOURS

        # Accumulate reach per hour-of-day
        hour_reach: Dict[int, List[int]] = {}
        for post in posts:
            ts_str = post.get("timestamp")
            reach = post.get("reach") or 0
            if not ts_str:
                continue
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                hour = dt.hour
                hour_reach.setdefault(hour, []).append(reach)
            except ValueError:
                continue

        if not hour_reach:
            return self._DEFAULT_HOURS

        avg_by_hour = {h: sum(v) / len(v) for h, v in hour_reach.items()}
        best_hours = sorted(avg_by_hour, key=lambda h: avg_by_hour[h], reverse=True)[:3]

        logger.info(f"Best posting hours from own insights: {best_hours}")
        return best_hours

    # ── Competitor scraping ───────────────────────────────────────────────────

    def _scrape_competitors(self) -> List[str]:
        """
        Read-only competitor scraping via instagrapi.

        Conservative settings:
          - Hard cap: max 2 competitor accounts (config ignored beyond 2)
          - Only 5 top posts per account
          - 15-25s human-like delay between accounts
          - Long cooldown after all accounts scraped
          - Uses IG_SCRAPE_USER / IG_SCRAPE_PASS (separate burner account)
        """
        try:
            from instagrapi import Client
            from instagrapi.exceptions import LoginRequired, PrivateAccount
        except ImportError:
            logger.warning("instagrapi not installed — skipping competitor scraping")
            return []

        ig_user = os.getenv("IG_SCRAPE_USER")
        ig_pass = os.getenv("IG_SCRAPE_PASS")

        if not ig_user or not ig_pass:
            logger.warning(
                "IG_SCRAPE_USER or IG_SCRAPE_PASS not set in .env — "
                "competitor scraping requires a secondary account"
            )
            return []

        # Hard max: 2 competitor accounts regardless of config
        competitors: List[str] = self.config.get("competitors", [])[:2]
        if not competitors:
            return []

        logger.info(f"Competitor scraping: {len(competitors)} account(s) [read-only]")

        cl = Client()
        cl.delay_range = [12, 22]  # 12-22s between instagrapi requests

        try:
            cl.login(ig_user, ig_pass)
        except Exception as exc:
            logger.warning(f"instagrapi login failed: {exc}")
            return []

        found_keywords: List[str] = []

        try:
            for account in competitors:
                time.sleep(random.uniform(15, 25))  # long delay before each account
                try:
                    user_id = cl.user_id_from_username(account)
                    medias = cl.user_medias(user_id, amount=5)  # only top 5

                    for media in medias:
                        caption_text = getattr(media, "caption_text", "") or ""
                        words = [
                            w.lower()
                            for w in caption_text.split()
                            if len(w) > 4
                            and not w.startswith("#")
                            and not w.startswith("@")
                        ]
                        found_keywords.extend(words[:3])

                    logger.info(f"Scraped @{account}: {len(medias)} posts analysed")

                except (PrivateAccount, LoginRequired) as exc:
                    logger.warning(f"Cannot access @{account}: {exc}")
                except Exception as exc:
                    logger.warning(f"Scrape failed for @{account}: {exc}")

                time.sleep(random.uniform(20, 40))  # long cooldown between accounts
        finally:
            try:
                cl.logout()
            except Exception:
                pass

        return list(set(found_keywords))[:10]
