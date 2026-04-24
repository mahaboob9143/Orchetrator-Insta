"""
agents/media_agent.py — MediaAgent for InstaAgent.

Responsibilities:
  1. Accept trending topics from TrendAgent.
  2. Search Unsplash for cinematic Islamic images matching each topic.
  3. Filter by minimum resolution (configurable, default 1080px).
  4. Dedup against all previously downloaded images using perceptual hashing
     (imagehash.phash — rejects if hamming distance < 10).
  5. Download approved images to media/queue/{hash}.jpg.
  6. Persist metadata to the images DB table.
  7. Respect Unsplash rate limit (50 req/hr free tier) with a local tracker.
"""

import os
import random
import time
from datetime import datetime, timedelta
from io import BytesIO
from typing import Dict, List, Optional, Tuple

import imagehash
import requests
from PIL import Image as PILImage

from core.db import (
    get_all_image_hashes,
    get_queued_image_count,
    save_image,
)
from core.flags import get_config
from core.logger import get_logger
from core.retry import retry

logger = get_logger("MediaAgent")

_UNSPLASH_BASE = "https://api.unsplash.com"
_PHASH_DISTANCE_THRESHOLD = 10   # images with distance < 10 are considered duplicates


class MediaAgent:
    """
    Downloads and queues cinematic Islamic images from Unsplash.
    """

    def __init__(self):
        self.config = get_config()
        self.unsplash_key: Optional[str] = os.getenv("UNSPLASH_ACCESS_KEY")
        # Track timestamps of Unsplash API requests for rate limiting
        self._request_timestamps: List[datetime] = []

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self, topics: List[str]) -> int:
        """
        Download images for the given topics until the queue is full.

        Args:
            topics: Ordered list of trending topics from TrendAgent.

        Returns:
            Number of newly downloaded images.
        """
        self.config = get_config()
        media_cfg = self.config.get("media", {})
        max_queue: int = media_cfg.get("max_queue_size", 20)
        queue_dir: str = media_cfg.get("queue_dir", "media/queue")
        min_res: int = media_cfg.get("min_resolution", 1080)

        os.makedirs(queue_dir, exist_ok=True)

        if not self.unsplash_key:
            logger.error("UNSPLASH_ACCESS_KEY not set in .env — MediaAgent cannot run")
            return 0

        current: int = get_queued_image_count()
        if current >= max_queue:
            logger.info(f"Queue full ({current}/{max_queue}) — skipping media fetch")
            return 0

        needed = max_queue - current
        logger.info(
            f"MediaAgent: queue has {current}/{max_queue} images — "
            f"fetching up to {needed} more for {len(topics)} topic(s)"
        )

        existing_hashes = get_all_image_hashes()
        downloaded = 0

        for topic in topics:
            if downloaded >= needed:
                break

            # Build 2-3 search variants per topic for better diversity
            search_terms = [
                f"{topic} Islamic cinematic",
                f"{topic} mosque architecture",
            ]

            for term in search_terms:
                if downloaded >= needed:
                    break

                try:
                    photos = self._search_unsplash(query=term, per_page=6)
                except Exception as exc:
                    logger.error(f"Unsplash search failed for '{term}': {exc}")
                    continue

                for photo in photos:
                    if downloaded >= needed:
                        break
                    result = self._process_photo(photo, queue_dir, min_res, existing_hashes)
                    if result:
                        existing_hashes.append(result)
                        downloaded += 1

                time.sleep(random.uniform(1.5, 3.5))  # pause between search terms

        logger.info(f"MediaAgent complete — {downloaded} new image(s) downloaded")
        return downloaded

    # ── Unsplash search ───────────────────────────────────────────────────────

    def _enforce_rate_limit(self) -> None:
        """
        Unsplash free tier: 50 requests per hour.
        Tracks request timestamps and sleeps if approaching the limit.
        """
        now = datetime.now()
        window_start = now - timedelta(hours=1)

        # Prune timestamps older than 1 hour
        self._request_timestamps = [
            t for t in self._request_timestamps if t > window_start
        ]

        if len(self._request_timestamps) >= 45:  # 5-request buffer before limit
            oldest = self._request_timestamps[0]
            wait_until = oldest + timedelta(hours=1)
            wait_secs = (wait_until - now).total_seconds()
            if wait_secs > 0:
                logger.warning(
                    f"Unsplash rate limit approaching "
                    f"({len(self._request_timestamps)}/50 used). "
                    f"Sleeping {wait_secs:.0f}s..."
                )
                time.sleep(wait_secs + 2)

        self._request_timestamps.append(datetime.now())

    @retry(
        max_attempts=3,
        backoff_factor=2,
        initial_wait=3.0,
        exceptions=(requests.RequestException,),
    )
    def _search_unsplash(self, query: str, per_page: int = 6) -> List[Dict]:
        """Search Unsplash and return a list of photo dicts."""
        self._enforce_rate_limit()

        headers = {"Authorization": f"Client-ID {self.unsplash_key}"}
        params = {
            "query": query,
            "per_page": per_page,
            "orientation": "portrait",
            "content_filter": "high",
            "order_by": "relevant",
        }

        resp = requests.get(
            f"{_UNSPLASH_BASE}/search/photos",
            headers=headers,
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    # ── Photo processing ──────────────────────────────────────────────────────

    def _process_photo(
        self,
        photo: Dict,
        queue_dir: str,
        min_res: int,
        existing_hashes: List[str],
    ) -> Optional[str]:
        """
        Validate, deduplicate, download, and save a single Unsplash photo.

        Args:
            photo:           Unsplash photo dict.
            queue_dir:       Directory to save the file.
            min_res:         Minimum pixel dimension (width or height).
            existing_hashes: All known perceptual hashes for dedup check.

        Returns:
            The phash string if the image was saved, else None.
        """
        try:
            width: int = photo.get("width", 0)
            height: int = photo.get("height", 0)
            photo_id: str = photo.get("id", "unknown")

            # Resolution filter
            if min(width, height) < min_res:
                logger.debug(
                    f"Skipping {photo_id} — too small ({width}×{height} < {min_res}px)"
                )
                return None

            # Aspect Ratio pre-filter (only download if it naturally fits or is an easy crop)
            aspect_ratio: float = width / height if height > 0 else 0
            # If it's way outside the 4:5 to 1.91:1 range, skip downloading entirely.
            # We allow 0.66 (2:3 aspect ratio common in portraits) to be cropped to 0.8 (4:5).
            if aspect_ratio < 0.66 or aspect_ratio > 2.0:
                logger.debug(f"Skipping {photo_id} — incompatible aspect ratio ({aspect_ratio:.2f})")
                return None

            # Download the full-size image
            download_url: str = photo["urls"]["full"]
            img_resp = requests.get(download_url, timeout=45)
            img_resp.raise_for_status()

            # Open image
            img = PILImage.open(BytesIO(img_resp.content)).convert("RGB")
            
            # ── Enforce Instagram Aspect Ratio (0.8 to 1.91) ──
            img_w, img_h = img.size
            aspect_ratio = img_w / img_h
            
            if aspect_ratio < 0.8:
                # Too tall (e.g., 2:3). Crop top and bottom to make it exactly 4:5
                new_h = int(img_w / 0.8)
                crop_y = (img_h - new_h) // 2
                img = img.crop((0, crop_y, img_w, crop_y + new_h))
            elif aspect_ratio > 1.91:
                # Too wide. Crop left and right to make it exactly 1.91:1
                new_w = int(img_h * 1.91)
                crop_x = (img_w - new_w) // 2
                img = img.crop((crop_x, 0, crop_x + new_w, img_h))

            # ── Resize to standard Instagram resolution (1080 max width) ──
            # thumbnail() preserves the new aspect ratio we just forced
            img.thumbnail((1080, 1350), PILImage.Resampling.LANCZOS)
            width, height = img.size

            # Compute perceptual hash on the FINAL cropped image
            phash_val = str(imagehash.phash(img))

            # Deduplication check against all known hashes
            for known_hash in existing_hashes:
                try:
                    dist = imagehash.hex_to_hash(phash_val) - imagehash.hex_to_hash(known_hash)
                    if dist < _PHASH_DISTANCE_THRESHOLD:
                        logger.debug(
                            f"Skipping {photo_id} — near-duplicate "
                            f"(phash distance={dist})"
                        )
                        return None
                except Exception:
                    continue

            # Save image as JPEG
            filename = f"{phash_val}.jpg"
            local_path = os.path.abspath(os.path.join(queue_dir, filename))
            img.convert("RGB").save(local_path, "JPEG", quality=95)

            # Persist to DB
            save_image(
                url=photo.get("links", {}).get("html", download_url),
                phash=phash_val,
                width=width,
                height=height,
                local_path=local_path,
                source="unsplash",
            )

            # Log Unsplash attribution (required by their API terms)
            photographer = photo.get("user", {}).get("name", "Unknown")
            photo_link = photo.get("links", {}).get("html", "")
            logger.info(
                f"Downloaded {photo_id} ({width}×{height}) → {filename} "
                f"| Photo by {photographer} ({photo_link})"
            )

            return phash_val

        except Exception as exc:
            logger.error(f"Failed to process photo {photo.get('id', '?')}: {exc}")
            return None
