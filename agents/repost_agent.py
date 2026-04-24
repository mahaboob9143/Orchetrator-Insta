"""
agents/repost_agent.py — RepostAgent for InstaAgent.

Responsibilities:
  1. Check the repost_enabled feature flag.
  2. Scrape recent image posts from configured PUBLIC source account(s)
     using instaloader — NO LOGIN REQUIRED for public profiles.
  3. Skip posts that have already been reposted (dedup via repost_log table).
  4. Download a random unseen post image to media/reposts/.
  5. Validate aspect ratio — skip incompatible ones.
  6. Rewrite the caption using template substitution (no AI needed).
  7. Log the source post to repost_log.
  8. Return a result dict ready for PosterAgent.

Usage:
  From orchestrator.repost_now() or directly:
    agent = RepostAgent()
    result = agent.run()
    if result:
        poster_agent.post(image=result["image"], caption=result["caption"], topic="repost")
"""

import os
import re
import random
import time
from io import BytesIO
from typing import Optional, Dict, Any, List

import requests
from PIL import Image as PILImage

from core.repost_tracker import is_reposted as is_post_reposted, mark_reposted as log_repost
from core.flags import get_config
from core.logger import get_logger
from core.caption_engine import build_caption

logger = get_logger("RepostAgent")

# ── Instagram-safe aspect ratio range ────────────────────────────────────────
_IG_MIN_RATIO = 0.66   # 2:3 can be cropped to 4:5 safely
_IG_MAX_RATIO = 1.91   # landscape max


class RepostAgent:
    """Scrapes a public Islamic Instagram account and reposts images with rewritten captions."""

    def __init__(self):
        self.config = get_config()

    # ── Public entry point ───────────────────────────────────────────────────

    def run(self) -> Optional[Dict[str, Any]]:
        """
        Main entry point. Checks flags, scrapes (no login), rewrites caption,
        and returns a result dict with 'image' and 'caption' keys ready for
        PosterAgent, or None if nothing actionable was found.
        """
        self.config = get_config()
        repost_cfg = self.config.get("repost", {})

        if not repost_cfg.get("enabled", False):
            logger.warning(
                "repost_enabled=false — repost skipped "
                "(set repost.enabled: true in config.yaml)"
            )
            return None

        source_accounts = repost_cfg.get("source_accounts", ["softeningsayings"])
        max_check = int(repost_cfg.get("max_posts_to_check", 20))
        download_dir = repost_cfg.get("download_dir", "media/reposts")
        add_credit = repost_cfg.get("add_credit_line", True)

        os.makedirs(download_dir, exist_ok=True)

        for username in source_accounts:
            result = self._process_account(
                username=username,
                max_check=max_check,
                download_dir=download_dir,
                add_credit=add_credit,
            )
            if result:
                return result

        logger.warning("No new unseen posts found across all source accounts.")
        return None

    # ── Scraping (authenticated via cookie, no challenge required) ─────────────

    def _get_session_id(self) -> Optional[str]:
        """
        Retrieve a valid Instagram session ID from:
          1. IG_SESSION_ID env var (URL-decoded in case it's %3A-encoded).
          2. .ig_session.json — the instagrapi session cache on disk.
        Returns None if neither is available.
        """
        from urllib.parse import unquote

        # Priority 1: env var
        raw = os.getenv("IG_SESSION_ID", "")
        if raw:
            return unquote(raw)

        # Priority 2: instagrapi cache file (written by previous username/pass login)
        session_file = ".ig_session.json"
        if os.path.exists(session_file):
            try:
                import json
                with open(session_file) as f:
                    settings = json.load(f)
                sid = (settings.get("cookies") or {}).get("sessionid")
                if sid:
                    logger.info("Reusing sessionid from cached .ig_session.json")
                    return sid
            except Exception as e:
                logger.debug(f"Could not read .ig_session.json: {e}")

        return None

    def _get_loader(self):
        """
        Build an instaloader instance.
        Injects a session cookie if available so Instagram doesn't 403 us.
        Falls back to fresh user/pass login, then anonymous (may be rate-limited).
        """
        import instaloader
        import io

        L = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            post_metadata_txt_pattern="",
            quiet=True,
        )

        # Suppress instaloader's internal 403-retry print messages —
        # they bypass our logger and spam the console. Our own logs still work fine.
        L.context.log_file = open(os.devnull, "w")

        session_id = self._get_session_id()
        username = os.getenv("IG_SCRAPE_USER", "")
        password = os.getenv("IG_SCRAPE_PASS", "")

        if session_id and username:
            # Inject cookie — authenticated without triggering login challenge
            L.context._session.cookies.set(
                "sessionid", session_id, domain=".instagram.com"
            )
            L.context.username = username
            logger.info(f"Instaloader authenticated via session cookie (@{username})")
        elif username and password:
            logger.info(f"Logging in as @{username} (username + password)...")
            try:
                L.login(username, password)
                logger.info("Login successful.")
            except Exception as e:
                logger.warning(f"Login failed: {e} — trying anonymous access")
        else:
            logger.warning(
                "No Instagram credentials found — anonymous access may be rate-limited. "
                "Add IG_SCRAPE_USER + IG_SCRAPE_PASS (or IG_SESSION_ID) to .env"
            )

        return L

    def _process_account(
        self,
        username: str,
        max_check: int,
        download_dir: str,
        add_credit: bool,
    ) -> Optional[Dict[str, Any]]:
        """Scrape a public account using an authenticated instaloader session."""
        import instaloader

        logger.info(f"Scraping profile @{username}...")

        try:
            L = self._get_loader()
            profile = instaloader.Profile.from_username(L.context, username)
        except Exception as exc:
            logger.error(f"Failed to access profile @{username}: {exc}")
            return None

        logger.info(
            f"@{username}: {profile.mediacount} total posts — "
            f"scanning up to {max_check} for unseen images..."
        )

        # Collect image posts up to max_check
        candidates = []
        try:
            for post in profile.get_posts():
                if post.is_video:
                    continue
                if post.typename not in ("GraphImage", "XDTGraphImage"):
                    continue  # skip carousels/sidecars for now
                candidates.append(post)
                if len(candidates) >= max_check:
                    break
        except Exception as exc:
            logger.error(f"Error iterating posts from @{username}: {exc}")

        if not candidates:
            logger.warning(f"No image posts found on @{username}")
            return None

        # Shuffle to avoid always picking the newest post
        random.shuffle(candidates)

        for post in candidates:
            post_id = str(post.shortcode)

            # Dedup — skip if already reposted
            if is_post_reposted(post_id):
                logger.debug(f"Already reposted {post_id} — skipping")
                continue

            result = self._download_and_prepare(
                post=post,
                username=username,
                download_dir=download_dir,
                add_credit=add_credit,
            )
            if result:
                return result

        logger.info(f"All checked posts from @{username} have already been reposted.")
        return None

    # ── Download + prepare ───────────────────────────────────────────────────

    def _download_and_prepare(
        self,
        post,
        username: str,
        download_dir: str,
        add_credit: bool,
    ) -> Optional[Dict[str, Any]]:
        """Download image, validate aspect ratio, rewrite caption, log to DB."""
        post_id = str(post.shortcode)

        try:
            img_url = post.url   # highest-res image URL from instaloader
            logger.info(f"Downloading post {post_id} from @{username}...")

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            }
            img_resp = requests.get(img_url, headers=headers, timeout=30)
            img_resp.raise_for_status()

            # Open and validate
            img = PILImage.open(BytesIO(img_resp.content)).convert("RGB")
            img_w, img_h = img.size
            aspect_ratio = img_w / img_h if img_h > 0 else 0

            # Pre-filter: skip images that are way outside Instagram's allowable range
            if aspect_ratio < _IG_MIN_RATIO or aspect_ratio > _IG_MAX_RATIO:
                logger.debug(
                    f"Skipping {post_id} — incompatible aspect ratio "
                    f"({aspect_ratio:.2f})"
                )
                # Log it so we don't re-check this post
                log_repost(
                    source_username=username,
                    source_post_id=post_id,
                    source_shortcode=post_id,
                    local_image_path="",
                    original_caption=post.caption or "",
                    rewritten_caption="[SKIPPED: incompatible aspect ratio]",
                )
                return None

            # Crop to Instagram-safe ratio + resize to max 1080px
            img = self._fit_to_instagram(img, img_w, img_h, aspect_ratio)
            final_w, final_h = img.size

            # Save to disk
            filename = f"repost_{post_id}.jpg"
            local_path = os.path.abspath(os.path.join(download_dir, filename))
            img.save(local_path, "JPEG", quality=95)

            logger.info(
                f"Downloaded @{username}/{post_id} -> {filename} "
                f"({final_w}x{final_h})"
            )

            # Rewrite caption
            original_caption = post.caption or ""
            rewritten = self._rewrite_caption(
                original=original_caption,
                add_credit=add_credit,
                credit_handle=username,
            )

            # Log to flat file tracker
            log_repost(post_id)

            logger.info("Caption rewritten. Ready to post.")

            image_dict = {
                "id": f"repost_{post_id}",
                "local_path": local_path,
                "width": final_w,
                "height": final_h,
                "source_post_id": post_id,
                "_cleanup_path": local_path,   # PosterAgent will delete after publish
            }

            return {
                "image": image_dict,
                "caption": rewritten,
                "source_post_id": post_id,
            }

        except Exception as exc:
            logger.error(f"Failed to process post {post_id}: {exc}", exc_info=True)
            return None

    # ── Image normalization ───────────────────────────────────────────────────

    def _fit_to_instagram(
        self, img: PILImage.Image, img_w: int, img_h: int, ratio: float
    ) -> PILImage.Image:
        """Crop to Instagram-safe aspect ratio and resize to max 1080px."""
        if ratio < 0.8:
            # Too tall — crop to 4:5
            new_h = int(img_w / 0.8)
            crop_y = (img_h - new_h) // 2
            img = img.crop((0, crop_y, img_w, crop_y + new_h))
        elif ratio > 1.91:
            # Too wide — crop to 1.91:1
            new_w = int(img_h * 1.91)
            crop_x = (img_w - new_w) // 2
            img = img.crop((crop_x, 0, crop_x + new_w, img_h))

        img.thumbnail((1080, 1350), PILImage.Resampling.LANCZOS)
        return img

    # ── Caption builder ───────────────────────────────────────────────────────

    def _rewrite_caption(
        self, original: str, add_credit: bool, credit_handle: str
    ) -> str:
        """
        Delegates to caption_engine.build_caption():
          1. Clean (strip hashtags, normalize whitespace)
          2. Classify via keyword scoring (sabr/shukr/tawakkul/akhirah/dua/general)
          3. Build [HOOK] + [BODY] + [EMOTIONAL LINE] + [CTA] + [HASHTAGS]
          4. Append credit line if requested
        """
        return build_caption(
            original=original,
            add_credit=add_credit,
            credit_handle=credit_handle,
        )
