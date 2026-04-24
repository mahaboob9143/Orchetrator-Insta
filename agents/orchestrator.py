"""
agents/orchestrator.py — Orchestrator for InstaAgent.

The Orchestrator coordinates all agents in the correct sequence,
manages the main autonomous loop, and handles failures gracefully.

Agent execution order per cycle:
  TrendAgent → MediaAgent → CaptionAgent → SchedulerAgent
  → [wait] → PosterAgent → EngagementAgent (background checks)

Key behaviours:
  - Feature flags are hot-reloaded each cycle (no restart needed).
  - Each agent failure is isolated: one agent failing doesn't crash
    the whole cycle — the orchestrator uses fallbacks where possible.
  - Three consecutive full-cycle failures → 1 hour pause + log alert.
  - The main loop sleeps in 60-second chunks so flag changes (e.g.
    setting posting_enabled=false) take effect promptly.
  - Background engagement checks are handled by APScheduler and run
    independently of the main orchestration loop.
"""

import time
import random
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from agents.caption_agent import CaptionAgent
from agents.engagement_agent import EngagementAgent
from agents.media_agent import MediaAgent
from agents.poster_agent import PosterAgent
from agents.repost_agent import RepostAgent
from agents.scheduler_agent import SchedulerAgent
from agents.trend_agent import TrendAgent, TrendOutput
from core.db import get_queued_images, get_recent_posts
from core.flags import get_flags, get_config
from core.logger import get_logger

logger = get_logger("Orchestrator")

# Fallback values used when TrendAgent fails
_FALLBACK_TOPICS = ["Islamic quotes", "Quran reflection", "Islamic reminder"]
_FALLBACK_CAPTION = (
    "سبحان الله — Glory be to Allah ☁️\n\n"
    "Every detail in His creation is a sign for those who reflect.\n\n"
    "Save this as a reminder 📌\n\n"
    "#Islam #Quran #Islamic #Allah #Muslim"
)


class Orchestrator:
    """
    Top-level coordinator for the InstaAgent multi-agent system.
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

        # Failure tracking for circuit-breaker-style pause
        self._consecutive_failures: int = 0
        self._max_consecutive_failures: int = 3

        # Shared background scheduler — engagement checks run here
        self._bg_scheduler = BackgroundScheduler(
            job_defaults={"coalesce": True, "max_instances": 1}
        )

        # Instantiate all agents
        self.trend_agent = TrendAgent()
        self.media_agent = MediaAgent()
        self.caption_agent = CaptionAgent()
        self.scheduler_agent = SchedulerAgent()
        self.poster_agent = PosterAgent()
        self.repost_agent = RepostAgent()
        self.engagement_agent = EngagementAgent(scheduler=self._bg_scheduler)

        logger.info(f"Orchestrator ready (dry_run={dry_run})")

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, once: bool = False) -> None:
        """
        Start the orchestration system.

        Args:
            once: If True, run a single cycle and exit. Used for --once CLI flag.
        """
        self._bg_scheduler.start()
        logger.info("Background scheduler started (engagement checks)")

        try:
            if once:
                logger.info("Running single cycle (--once mode)...")
                self._run_cycle()
            else:
                logger.info("Starting autonomous loop — Ctrl+C to stop")
                self._autonomous_loop()
        finally:
            logger.info("Shutting down background scheduler...")
            self._bg_scheduler.shutdown(wait=False)
            logger.info("Orchestrator stopped")

    def run_single_agent(self, agent_name: str) -> None:
        """
        Invoke a single agent for isolated testing.
        Used with the `--agent <name>` CLI flag.
        """
        self._bg_scheduler.start()
        try:
            dispatch = {
                "trend": self._test_trend,
                "media": self._test_media,
                "caption": self._test_caption,
                "scheduler": self._test_scheduler,
                "engagement": self._test_engagement,
            }
            fn = dispatch.get(agent_name)
            if fn is None:
                logger.error(
                    f"Unknown agent '{agent_name}'. "
                    f"Valid options: {sorted(dispatch.keys())}"
                )
                return
            logger.info(f"Running single agent: {agent_name}")
            fn()
        finally:
            self._bg_scheduler.shutdown(wait=False)

    # ── Autonomous loop ───────────────────────────────────────────────────────

    def repost_now(self) -> None:
        """
        Repost mode (--repost).

        Scrapes one unseen image from the configured source account,
        rewrites the caption, and posts to Instagram. Then exits cleanly.
        No background scheduler, no engagement tracking.
        """
        logger.info("=" * 60)
        logger.info("  REPOST NOW — scrape & publish pipeline")
        logger.info("=" * 60)

        # ── Step 1: Scrape + prepare ───────────────────────────────────────
        logger.info("RepostAgent: fetching post from source account...")
        result = self.repost_agent.run()

        if not result:
            logger.error(
                "RepostAgent returned nothing. "
                "Check repost.enabled=true in config.yaml "
                "and that IG_SCRAPE_USER / IG_SCRAPE_PASS are set in .env."
            )
            return

        image = result["image"]
        caption = result["caption"]
        source_post_id = result["source_post_id"]

        logger.info(f"Repost ready — source post: {source_post_id}")
        logger.info(f"Caption preview:\n{caption[:300]}...")

        # ── Step 2: Publish via PosterAgent ───────────────────────────────
        logger.info("Posting to Instagram now...")
        ig_post_id: Optional[str] = self.poster_agent.post(
            image=image,
            caption=caption,
            topic="repost",
        )

        if not ig_post_id:
            logger.error("Post failed. Check logs/errors.log for details.")
            return

        # ── Step 3: Record published post ID in DB ─────────────────────────
        from core.db import update_repost_ig_id
        update_repost_ig_id(source_post_id=source_post_id, ig_post_id=ig_post_id)

        logger.info(f"Repost complete. IG post ID: {ig_post_id}")

    def post_now(self, image_url: Optional[str] = None) -> None:
        """
        Immediate test-post mode (--post-now).

        Bypasses scheduler entirely. Validates the full pipeline fast:
          1. Fake image using URL, or fetch one image from Unsplash.
          2. Build a template caption (no Ollama — instant, no wait).
          3. Post to Instagram right now.
          4. Schedule background engagement checks.
        """
        self._bg_scheduler.start()
        logger.info("=" * 60)
        logger.info("  POST NOW — instant end-to-end test")
        logger.info("=" * 60)

        try:
            # ── Step 1: Get an image ───────────────────────────────────────
            topic = "Islamic reminder"
            if image_url:
                image = {"id": 999999, "local_path": "external_url_test", "width": 0, "height": 0}
                logger.info(f"Using explicit external image URL: {image_url}")
            else:
                queued = get_queued_images()
                if not queued:
                    logger.info("Queue empty — fetching one image from Unsplash...")
                    try:
                        self.media_agent.run(["Islamic mosque cinematic", "Quran beautiful"])
                        queued = get_queued_images()
                    except Exception as exc:
                        logger.error(f"MediaAgent failed: {exc}")

                if not queued:
                    logger.error(
                        "❌ No images available. "
                        "Check your UNSPLASH_ACCESS_KEY in .env and try again."
                    )
                    return

                image = queued[0]
                logger.info(
                    f"Image ready: {image.get('local_path', 'N/A')} "
                    f"({image.get('width')}×{image.get('height')} px)"
                )

            # ── Step 2: Template caption (no Ollama — instant) ────────────
            logger.info("Generating template caption (Ollama skipped)...")
            caption = self.caption_agent._template_caption(topic)
            logger.info(f"Caption:\n{caption}\n")

            # ── Step 3: Post ───────────────────────────────────────────────
            logger.info("Posting to Instagram now...")
            ig_post_id: Optional[str] = self.poster_agent.post(
                image=image,
                caption=caption,
                topic=topic,
                override_image_url=image_url
            )

            if not ig_post_id:
                logger.error(
                    "❌ Post failed. Check logs/errors.log for details.\n"
                    "   Common causes:\n"
                    "   - Invalid META_ACCESS_TOKEN or IG_ACCOUNT_ID in .env\n"
                    "   - Port 8765 not reachable from the internet\n"
                    "   - image_server.public_base_url not set correctly in config.yaml"
                )
                return

            logger.info(f"\n✅ SUCCESS! Instagram post ID: {ig_post_id}")
            logger.info("   Check your Instagram profile — the post should be live now.")

            # ── Step 4: Schedule engagement checks ────────────────────────
            try:
                recent = get_recent_posts(limit=1)
                post_id = recent[0]["id"] if recent else None
                if post_id:
                    self.engagement_agent.schedule_checks(
                        ig_post_id=ig_post_id,
                        post_id=post_id,
                        posted_at=datetime.now(),
                    )
                    logger.info(
                        "Engagement checks scheduled at 1hr / 6hr / 24hr.\n"
                        "Keep this process running to collect them, or re-run later."
                    )
            except Exception as exc:
                logger.warning(f"Engagement scheduling failed (non-critical): {exc}")

        except KeyboardInterrupt:
            logger.info("Interrupted")
        finally:
            self._bg_scheduler.shutdown(wait=False)

    def _autonomous_loop(self) -> None:
        """
        Main indefinite loop — continues until KeyboardInterrupt.
        """
        while True:
            try:
                flags = get_flags()

                # ── Kill switch ────────────────────────────────────────────
                if not flags.posting_enabled and not self.dry_run:
                    logger.info(
                        "posting_enabled=false — pausing for 5 min (change flag to resume)"
                    )
                    self._chunked_sleep(300)
                    continue

                # ── Daily limit check ──────────────────────────────────────
                if not self.scheduler_agent.should_post_now():
                    next_time = self.scheduler_agent.get_next_posting_time()
                    wait = self.scheduler_agent.seconds_until(next_time)
                    logger.info(
                        f"Daily post limit reached. "
                        f"Sleeping {wait / 3600:.1f}hr until "
                        f"{next_time.strftime('%H:%M')} "
                        f"(next post window)"
                    )
                    # Sleep in 1hr chunks so kill-switch takes effect quickly
                    self._chunked_sleep(min(wait, 3600))
                    continue

                # ── Calculate next post time ───────────────────────────────
                next_post_time = self.scheduler_agent.get_next_posting_time()
                wait_secs = self.scheduler_agent.seconds_until(next_post_time)

                # ── Prep cycle while waiting ───────────────────────────────
                if wait_secs > 120:
                    logger.info(
                        f"Next post at {next_post_time.strftime('%H:%M')} "
                        f"({wait_secs / 3600:.1f}hr away). "
                        "Running trend + media prep..."
                    )
                    self._run_prep_cycle()
                    # Sleep the remainder
                    remaining = self.scheduler_agent.seconds_until(next_post_time)
                    self._chunked_sleep(remaining)

                # ── Full posting cycle ─────────────────────────────────────
                success = self._run_cycle()

                if success:
                    self._consecutive_failures = 0
                else:
                    self._consecutive_failures += 1
                    logger.warning(
                        f"Cycle failed "
                        f"({self._consecutive_failures}/{self._max_consecutive_failures})"
                    )
                    if self._consecutive_failures >= self._max_consecutive_failures:
                        logger.error(
                            f"🚨 {self._consecutive_failures} consecutive failures. "
                            "Pausing 1 hour before retrying..."
                        )
                        self._chunked_sleep(3600)
                        self._consecutive_failures = 0

            except KeyboardInterrupt:
                logger.info("Interrupted by user — shutting down gracefully")
                break
            except Exception as exc:
                logger.error(f"Unexpected orchestrator error: {exc}", exc_info=True)
                self._chunked_sleep(300)   # 5 min pause on unexpected errors

    # ── Cycle execution ───────────────────────────────────────────────────────

    def _run_prep_cycle(self) -> None:
        """
        Trend + media only (no posting).
        Keeps the image queue stocked while waiting for the next posting window.
        """
        logger.info("--- Prep Cycle: Trend + Media ---")
        try:
            trends = self.trend_agent.run()
        except Exception as exc:
            logger.error(f"TrendAgent failed in prep cycle: {exc}")
            trends = TrendOutput(topics=_FALLBACK_TOPICS, best_hours=[8, 14, 21])
        try:
            self.media_agent.run(trends.topics)
        except Exception as exc:
            logger.error(f"MediaAgent failed in prep cycle: {exc}")

    def _run_cycle(self) -> bool:
        """
        Execute one full orchestration cycle.

        Returns:
            True if a post was published (or simulated in dry-run), False otherwise.
        """
        logger.info("━" * 60)
        logger.info("Starting full orchestration cycle")
        logger.info("━" * 60)

        # ── 1. TrendAgent ──────────────────────────────────────────────────
        try:
            trends: TrendOutput = self.trend_agent.run()
        except Exception as exc:
            logger.error(f"TrendAgent failed: {exc}")
            trends = TrendOutput(topics=_FALLBACK_TOPICS, best_hours=[8, 14, 21])

        # ── 2. MediaAgent ──────────────────────────────────────────────────
        try:
            self.media_agent.run(trends.topics)
        except Exception as exc:
            logger.error(f"MediaAgent failed: {exc}")

        # ── 3. Select image from queue ─────────────────────────────────────
        queued = get_queued_images()
        if not queued:
            logger.warning("No queued images — cannot post this cycle")
            return False

        image = queued[0]    # oldest first
        topic = trends.topics[0] if trends.topics else "Islamic reminder"

        # ── 4. CaptionAgent ────────────────────────────────────────────────
        try:
            caption = self.caption_agent.run(topic=topic, image_metadata=image)
        except Exception as exc:
            logger.error(f"CaptionAgent failed: {exc}")
            caption = _FALLBACK_CAPTION

        # ── 5. Dry-run shortcut ────────────────────────────────────────────
        if self.dry_run:
            logger.info("[DRY RUN] Cycle complete — would post:")
            logger.info(f"  Image  : {image.get('local_path', 'N/A')}")
            logger.info(f"  Topic  : {topic}")
            logger.info(f"  Caption: {caption[:120]}...")
            return True

        # ── 6. PosterAgent ─────────────────────────────────────────────────
        try:
            ig_post_id: Optional[str] = self.poster_agent.post(
                image=image,
                caption=caption,
                topic=topic,
            )
        except Exception as exc:
            logger.error(f"PosterAgent failed: {exc}", exc_info=True)
            return False

        if not ig_post_id:
            return False

        # ── 7. Schedule engagement checks ──────────────────────────────────
        try:
            recent = get_recent_posts(limit=1)
            post_id = recent[0]["id"] if recent else None
            if post_id:
                self.engagement_agent.schedule_checks(
                    ig_post_id=ig_post_id,
                    post_id=post_id,
                    posted_at=datetime.now(),
                )
        except Exception as exc:
            logger.error(f"Engagement scheduling failed (non-critical): {exc}")

        logger.info("✅ Cycle complete!")
        return True

    # ── Sleep helper ──────────────────────────────────────────────────────────

    def _chunked_sleep(self, seconds: float) -> None:
        """
        Sleep in 60-second chunks so we can respond quickly to flag changes
        (e.g. setting posting_enabled=false while sleeping).
        """
        remaining = max(0.0, seconds)
        while remaining > 0:
            chunk = min(60.0, remaining)
            time.sleep(chunk)
            remaining -= chunk

            # Re-check kill switch mid-sleep
            if not get_flags().posting_enabled and not self.dry_run:
                logger.info("Kill switch activated mid-sleep — waking up")
                break

    # ── Single-agent test helpers ─────────────────────────────────────────────

    def _test_trend(self) -> None:
        out = self.trend_agent.run()
        logger.info(f"TrendAgent output: topics={out.topics} hours={out.best_hours}")

    def _test_media(self) -> None:
        count = self.media_agent.run(["Islamic quotes", "Quran reflection"])
        logger.info(f"MediaAgent: downloaded {count} image(s)")

    def _test_caption(self) -> None:
        caption = self.caption_agent.run("Islamic reminder", {})
        logger.info(f"CaptionAgent output:\n{caption}")

    def _test_scheduler(self) -> None:
        allowed = self.scheduler_agent.should_post_now()
        next_t = self.scheduler_agent.get_next_posting_time()
        logger.info(f"SchedulerAgent: can_post={allowed}, next={next_t}")

    def _test_engagement(self) -> None:
        logger.info(
            "EngagementAgent requires a live ig_post_id. "
            "Schedule a real check via the orchestrator after posting."
        )
