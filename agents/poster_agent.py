"""
agents/poster_agent.py — PosterAgent for InstaAgent Repost Pipeline.

Responsibilities:
  1. Upload image to Cloudinary to get a public URL.
  2. Execute the 3-step Meta Graph API posting flow:
       a. POST /{account}/media  → create container
       b. Poll container status  → wait for FINISHED
       c. POST /{account}/media_publish → publish
  3. Delete image from Cloudinary to keep it clean.
"""

import os
import time
import random
from typing import Optional

import json
import requests

from core.flags import get_config
from core.logger import get_logger
from core.retry import retry
from core.cloudinary_uploader import upload_image, delete_image
from core.story_designer import create_story_image

logger = get_logger("PosterAgent")

_GRAPH_API_BASE = "https://graph.facebook.com/v19.0"


class PosterAgent:
    """
    Publishes approved images to Instagram via the official Meta Graph API.
    """

    def __init__(self):
        self.access_token: Optional[str] = os.getenv("META_ACCESS_TOKEN")
        self.ig_account_id: Optional[str] = os.getenv("IG_ACCOUNT_ID")

    # ── Public entry point ────────────────────────────────────────────────────

    def post(self, image: dict, caption: str, topic: str = "") -> Optional[str]:
        """
        Post an image to Instagram.

        Args:
            image:              Image dict from RepostAgent.
            caption:            Generated caption string.
            topic:              Topic string for logs.

        Returns:
            Instagram post ID string on success, None on failure.
        """
        if not self.access_token or not self.ig_account_id:
            logger.error("META_ACCESS_TOKEN or IG_ACCOUNT_ID missing from .env")
            return None

        local_path: str = image.get("local_path", "")
        if not local_path or not os.path.isfile(local_path):
            logger.error(f"Image file not found: '{local_path}'")
            return None

        ig_post_id: Optional[str] = None
        cloud_public_id: Optional[str] = None

        try:
            logger.info("Using Cloudinary for image hosting (cloud native)...")
            image_url, cloud_public_id = upload_image(local_path)
            if not image_url:
                logger.error("Cloudinary upload failed. Cannot post.")
                return None
                
            # 1. Post to Feed
            ig_post_id = self._publish(image_url=image_url, caption=caption)
            
            # 2. Post to Story (if enabled)
            config = get_config()
            if ig_post_id and config.get("repost", {}).get("post_to_story", False):
                logger.info("Story mode enabled — sharing to Instagram Story...")
                self._publish_story(local_path=local_path)
        except Exception as exc:
            logger.error(f"Post failed: {exc}", exc_info=True)
            return None
        finally:
            # ── Cleanup Cloudinary ───────────────────────────────────────────
            if cloud_public_id:
                delete_image(cloud_public_id)

            # ── Local File Cleanup (Always happens) ──────────────────────────
            cleanup_path = image.get("_cleanup_path") or image.get("local_path")
            if cleanup_path and os.path.exists(cleanup_path):
                try:
                    os.remove(cleanup_path)
                    logger.info(f"Cleaned up local file: {os.path.basename(cleanup_path)}")
                except Exception as e:
                    logger.warning(f"Could not delete local repost file: {e}")

        if ig_post_id:
            logger.info(f"✅ Repost published! IG post ID: {ig_post_id}")
            
        return ig_post_id

    # ── Meta Graph API flow ───────────────────────────────────────────────────

    def _publish(self, image_url: str, caption: str) -> Optional[str]:
        """Full 3-step Meta posting flow."""

        # Step 1: Create media container
        logger.info("Meta API [1/3]: creating media container...")
        container_id = self._create_container(image_url=image_url, caption=caption)
        if not container_id:
            return None

        # Step 2: Poll until container status = FINISHED
        time.sleep(random.uniform(3.0, 6.0))   # human-like delay
        logger.info("Meta API [2/3]: waiting for container to be ready...")
        ready = self._await_container(container_id, max_wait_secs=90)
        if not ready:
            logger.error(f"Container {container_id} did not reach FINISHED in time")
            return None

        # Step 3: Publish
        time.sleep(random.uniform(2.0, 5.0))   # human-like delay
        logger.info("Meta API [3/3]: publishing container...")
        ig_post_id = self._publish_container(container_id)
        return ig_post_id

    @retry(
        max_attempts=3,
        backoff_factor=2,
        initial_wait=5.0,
        exceptions=(requests.RequestException,),
    )
    def _create_container(self, image_url: str, caption: str) -> Optional[str]:
        """POST /{account_id}/media — create an IG media container."""
        url = f"{_GRAPH_API_BASE}/{self.ig_account_id}/media"
        
        # Get collaborators from config
        config = get_config()
        collabs = config.get("repost", {}).get("collaborators", [])
        
        data = {
            "image_url": image_url,
            "caption": caption,
            "access_token": self.access_token,
        }
        
        if collabs:
            # Meta expects a JSON-encoded list of strings
            data["collaborators"] = json.dumps(collabs)
            logger.info(f"Inviting collaborators: {', '.join(collabs)}")

        resp = requests.post(url, data=data, timeout=30)

        if not resp.ok:
            try:
                err_body = resp.json()
                error = err_body.get("error", {})
                code = error.get("code")
                msg = error.get("message", "")
                subcode = error.get("error_subcode", "")
                logger.error(
                    f"Meta API rejected request — "
                    f"HTTP {resp.status_code} | code={code} subcode={subcode} | {msg}"
                )

                if code == 10:
                    logger.error("→ Fix: Check your access token scopes.")
                    return None
                if code == 190:
                    logger.critical("→ Fix: Token expired.")
                    return None
            except Exception:
                logger.error(f"Meta API HTTP {resp.status_code}: {resp.text[:500]}")

        resp.raise_for_status()
        container_id = resp.json().get("id")
        logger.info(f"Container created: {container_id}")
        return container_id

    def _await_container(self, container_id: str, max_wait_secs: int = 90) -> bool:
        """Poll container status until FINISHED or ERROR."""
        url = f"{_GRAPH_API_BASE}/{container_id}"
        params = {
            "fields": "status_code",
            "access_token": self.access_token,
        }

        waited = 0
        while waited < max_wait_secs:
            try:
                resp = requests.get(url, params=params, timeout=15)
                resp.raise_for_status()
                status = resp.json().get("status_code", "")
                
                if status == "FINISHED":
                    return True
                if status == "ERROR":
                    logger.error(f"Container {container_id} processing ERROR")
                    return False
            except requests.RequestException as exc:
                logger.warning(f"Container poll error: {exc}")

            time.sleep(5)
            waited += 5

        return False

    @retry(
        max_attempts=3,
        backoff_factor=2,
        initial_wait=5.0,
        exceptions=(requests.RequestException,),
    )
    def _publish_container(self, container_id: str) -> Optional[str]:
        """POST /{account_id}/media_publish — publish a ready container."""
        url = f"{_GRAPH_API_BASE}/{self.ig_account_id}/media_publish"
        data = {
            "creation_id": container_id,
            "access_token": self.access_token,
        }

        resp = requests.post(url, data=data, timeout=30)
        resp.raise_for_status()

        post_id = resp.json().get("id")
        return post_id

    # ── Story Publishing ──────────────────────────────────────────────────────

    def _publish_story(self, local_path: str) -> bool:
        """Full flow for Story publishing with Auto-Designer."""
        story_local = "media/temp_story.jpg"
        story_cloud_id = None
        
        try:
            # 1. Design the Story Image (Option A: Blur + Text)
            logger.info("Designing vertical Story image...")
            success = create_story_image(local_path, story_local, text="NEW POST")
            if not success:
                return False

            # 2. Upload Story image to Cloudinary
            logger.info("Uploading designed Story to Cloudinary...")
            story_url, story_cloud_id = upload_image(story_local)
            if not story_url:
                return False

            # 3. Create Story container
            logger.info("Meta API: creating Story media container...")
            container_id = self._create_story_container(story_url)
            if not container_id:
                return False

            # 4. Wait
            time.sleep(random.uniform(3.0, 6.0))
            ready = self._await_container(container_id, max_wait_secs=90)
            if not ready:
                return False

            # 5. Publish
            logger.info("Meta API: publishing Story container...")
            story_id = self._publish_container(container_id)
            
            if story_id:
                logger.info(f"✅ Shared to Story! Story ID: {story_id}")
                return True
        except Exception as e:
            logger.error(f"Story publish failed: {e}")
        finally:
            # Cleanup story-specific temp files
            if story_cloud_id:
                delete_image(story_cloud_id)
            if os.path.exists(story_local):
                os.remove(story_local)
        
        return False

    @retry(
        max_attempts=3,
        backoff_factor=2,
        initial_wait=5.0,
        exceptions=(requests.RequestException,),
    )
    def _create_story_container(self, image_url: str) -> Optional[str]:
        """POST /{account_id}/media — create a Story container."""
        url = f"{_GRAPH_API_BASE}/{self.ig_account_id}/media"
        data = {
            "image_url": image_url,
            "media_type": "STORIES",
            "access_token": self.access_token,
        }

        resp = requests.post(url, data=data, timeout=30)
        
        if not resp.ok:
            logger.error(f"Story container failed: {resp.text}")
            return None

        resp.raise_for_status()
        return resp.json().get("id")
