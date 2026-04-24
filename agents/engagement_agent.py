"""
agents/engagement_agent.py — EngagementAgent for InstaAgent.

Responsibilities:
  1. Schedule background engagement checks at 1hr, 6hr, and 24hr
     after each post using APScheduler (BackgroundScheduler).
  2. Fetch likes, comments, shares, reach, and saves per post
     from the Meta Graph API.
  3. Persist each snapshot to the engagement table.
  4. Flag high-performing posts and update posting_windows with
     time→reach correlation data for the SchedulerAgent to learn from.
"""

import os
from datetime import datetime, timedelta
from typing import Optional

import requests
from apscheduler.schedulers.background import BackgroundScheduler

from core.db import get_recent_posts, save_engagement, upsert_posting_window
from core.flags import get_config, get_flags
from core.logger import get_logger
from core.retry import retry

logger = get_logger("EngagementAgent")

_GRAPH_API_BASE = "https://graph.facebook.com/v19.0"


class EngagementAgent:
    """
    Schedules and executes post-engagement tracking using a shared
    APScheduler BackgroundScheduler.
    """

    def __init__(self, scheduler: BackgroundScheduler):
        self.config = get_config()
        self.flags = get_flags()
        self.scheduler = scheduler
        self.access_token: Optional[str] = os.getenv("META_ACCESS_TOKEN")

    # ── Public entry point ────────────────────────────────────────────────────

    def schedule_checks(
        self,
        ig_post_id: str,
        post_id: int,
        posted_at: datetime,
    ) -> None:
        """
        Schedule background engagement checks at configured intervals.

        Args:
            ig_post_id: Instagram-side post ID (returned by media_publish).
            post_id:    Internal DB posts.id for this post.
            posted_at:  Datetime when the post was published.
        """
        # Hot-reload
        self.config = get_config()
        self.flags = get_flags()

        if not self.flags.engagement_analysis:
            logger.info("engagement_analysis=false — skipping engagement scheduling")
            return

        intervals: list = self.config.get("engagement", {}).get(
            "check_intervals_hours", [1, 6, 24]
        )

        for hours in intervals:
            run_at = posted_at + timedelta(hours=hours)
            job_id = f"eng_{ig_post_id}_{hours}h"

            self.scheduler.add_job(
                func=self._check_engagement,
                trigger="date",
                run_date=run_at,
                id=job_id,
                args=[ig_post_id, post_id, hours],
                misfire_grace_time=3600,   # tolerate up to 1hr if system was sleeping
                replace_existing=True,
            )
            logger.info(
                f"Engagement check scheduled: {job_id} "
                f"at {run_at.strftime('%Y-%m-%d %H:%M')}"
            )

    # ── Background job ────────────────────────────────────────────────────────

    def _check_engagement(
        self, ig_post_id: str, post_id: int, interval_hours: int
    ) -> None:
        """
        Fetch and store an engagement snapshot. Called by APScheduler.
        This method runs in a background thread — must not block indefinitely.
        """
        logger.info(
            f"Engagement check | post={ig_post_id} | interval={interval_hours}hr"
        )

        try:
            metrics = self._fetch_metrics(ig_post_id)
        except Exception as exc:
            logger.error(
                f"Engagement fetch failed for {ig_post_id} ({interval_hours}hr): {exc}"
            )
            return

        if not metrics:
            logger.warning(f"No metrics returned for post {ig_post_id}")
            return

        likes: int = metrics.get("like_count", 0) or 0
        comments: int = metrics.get("comments_count", 0) or 0
        reach: int = metrics.get("reach", 0) or 0
        saves: int = metrics.get("saved", 0) or 0
        # 'shares' is not available on all account types via Graph API
        shares: int = (
            metrics["shares"]["count"]
            if isinstance(metrics.get("shares"), dict)
            else 0
        )

        # Determine if high performer
        thresholds = self.config.get("engagement", {}).get(
            "high_performance_threshold", {}
        )
        is_high = (
            likes >= thresholds.get("likes", 500)
            or reach >= thresholds.get("reach", 5000)
        )

        save_engagement(
            post_id=post_id,
            interval_hours=interval_hours,
            likes=likes,
            comments=comments,
            shares=shares,
            reach=reach,
            saves=saves,
            is_high_performer=is_high,
        )

        # At 24hr interval: update posting-window learning table
        if interval_hours == 24 and reach > 0:
            self._update_posting_window(post_id=post_id, reach=reach)

        star = "⭐ HIGH PERFORMER" if is_high else "—"
        logger.info(
            f"Post {ig_post_id} [{interval_hours}hr] "
            f"likes={likes} comments={comments} reach={reach} saves={saves} {star}"
        )

    # ── Meta Graph API ────────────────────────────────────────────────────────

    @retry(
        max_attempts=3,
        backoff_factor=2,
        initial_wait=5.0,
        exceptions=(requests.RequestException,),
    )
    def _fetch_metrics(self, ig_post_id: str) -> Optional[dict]:
        """Fetch engagement fields for a single post via Graph API."""
        url = f"{_GRAPH_API_BASE}/{ig_post_id}"
        params = {
            "fields": "like_count,comments_count,shares,reach,saved",
            "access_token": self.access_token,
        }

        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── Learning ──────────────────────────────────────────────────────────────

    def _update_posting_window(self, post_id: int, reach: float) -> None:
        """
        Update the posting_windows table with the reach achieved at
        the hour this post was published.

        This feeds the SchedulerAgent's data-driven hour selection.
        """
        try:
            recent = get_recent_posts(limit=20)
            post = next((p for p in recent if p["id"] == post_id), None)
            if not post:
                return

            posted_at_str = post.get("posted_at", "")
            if not posted_at_str:
                return

            posted_dt = datetime.fromisoformat(posted_at_str)
            upsert_posting_window(hour=posted_dt.hour, reach=reach)
            logger.info(
                f"Posting window updated: hour={posted_dt.hour} "
                f"new_reach_sample={reach:.0f}"
            )
        except Exception as exc:
            logger.warning(f"Failed to update posting window: {exc}")
