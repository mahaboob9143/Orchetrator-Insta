"""
agents/scheduler_agent.py — SchedulerAgent for InstaAgent.

Responsibilities:
  1. Determine whether posting is allowed today (daily cap check).
  2. Calculate the optimal next posting datetime:
     - Prefers hours learned from historical engagement data (posting_windows table).
     - Falls back to configured posting windows in config.yaml.
     - Applies a random ± offset to simulate human posting behaviour.
  3. Expose helpers used by the Orchestrator for sleep/wait logic.
"""

import random
from datetime import datetime, timedelta
from typing import List, Optional

from core.db import get_best_posting_hours, posts_today_count
from core.flags import get_config
from core.logger import get_logger

logger = get_logger("SchedulerAgent")


class SchedulerAgent:
    """
    Decides when the next post should go out.
    """

    def __init__(self):
        self.config = get_config()

    # ── Public API ────────────────────────────────────────────────────────────

    def should_post_now(self) -> bool:
        """
        Check whether we are allowed to post another time today.

        Returns False if today's post count >= max_posts_per_day.
        """
        self.config = get_config()
        max_per_day: int = self.config.get("schedule", {}).get("max_posts_per_day", 2)
        today: int = posts_today_count()

        if today >= max_per_day:
            logger.info(
                f"Daily limit reached: {today}/{max_per_day} posts today — skipping"
            )
            return False

        logger.info(f"Posts today: {today}/{max_per_day} — posting allowed")
        return True

    def get_next_posting_time(self) -> datetime:
        """
        Calculate the next optimal posting datetime.

        Priority:
          1. Hours learned from engagement data (posting_windows table,
             requires ≥ 2 data points per hour).
          2. Configured posting windows in config.yaml.

        A random offset is applied within the selected window to avoid
        posting at the exact same time every day (more human-like).

        Returns:
            A future datetime for the next post.
        """
        self.config = get_config()
        schedule = self.config.get("schedule", {})
        offset_mins: int = schedule.get("random_offset_minutes", 30)

        # Try DB-learned best hours first
        best_hours: List[int] = get_best_posting_hours(n=3)

        if best_hours:
            target_hour = random.choice(best_hours)
            logger.info(f"Using DB-learned hour: {target_hour}:00")
        else:
            target_hour = self._pick_from_config_windows(schedule)
            logger.info(f"Using config window hour: {target_hour}:00")

        # Build target datetime with random offset
        now = datetime.now()
        target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        offset_secs = random.randint(-offset_mins * 60, offset_mins * 60)
        target += timedelta(seconds=offset_secs)

        # Ensure the target is in the future
        if target <= now:
            # Move to the same window tomorrow
            target += timedelta(days=1)
            # Use a fresh window hour for tomorrow
            target_hour = self._pick_from_config_windows(schedule)
            target = target.replace(hour=target_hour, minute=0, second=0, microsecond=0)
            # Only positive offset when rescheduling to tomorrow
            target += timedelta(minutes=random.randint(0, offset_mins))

        logger.info(f"Next post scheduled: {target.strftime('%Y-%m-%d %H:%M')}")
        return target

    def seconds_until(self, target: datetime) -> float:
        """Return non-negative seconds remaining until target."""
        delta = target - datetime.now()
        return max(0.0, delta.total_seconds())

    def get_today_post_count(self) -> int:
        return posts_today_count()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _pick_from_config_windows(self, schedule: dict) -> int:
        """
        Pick a random hour from a randomly chosen config posting window.

        Falls back to [8, 14, 21] if config has no windows defined.
        """
        windows: dict = schedule.get(
            "posting_windows",
            {
                "morning": ["07:00", "09:00"],
                "afternoon": ["13:00", "15:00"],
                "evening": ["20:00", "22:00"],
            },
        )

        window_name = random.choice(list(windows.keys()))
        window = windows[window_name]

        try:
            start_h = int(window[0].split(":")[0])
            end_h = int(window[1].split(":")[0])
            hour = random.randint(start_h, end_h)
        except (IndexError, ValueError):
            logger.warning("Invalid posting window config — using fallback hour 8")
            hour = 8

        logger.debug(f"Selected '{window_name}' window → posting hour {hour}:00")
        return hour
