"""
agents/poster_agent.py — PosterAgent for InstaAgent.

Responsibilities:
  1. Check feature flags (posting_enabled, human_approval).
  2. Serve the image locally via http.server (core/server.py).
  3. Execute the 3-step Meta Graph API posting flow:
       a. POST /{account}/media  → create container
       b. Poll container status  → wait for FINISHED
       c. POST /{account}/media_publish → publish
  4. Log every post to SQLite and mark image as posted.
  5. Handle known Graph API error codes without retrying.

IMPORTANT — Public IP Requirement:
  Meta's servers must be able to download the image from your machine.
  Set `image_server.public_base_url` in config.yaml to your machine's
  public IP or domain. If behind NAT, forward port 8765 → local IP.
"""

import os
import time
import random
from typing import Optional

import requests

from core.db import mark_image_posted, save_post
from core.flags import get_config, get_flags
from core.logger import get_logger
from core.retry import retry
from core.server import ImageServer
from core.tunnel import TunneledImageServer
from core.cloudinary_uploader import upload_image, delete_image

logger = get_logger("PosterAgent")

_GRAPH_API_BASE = "https://graph.facebook.com/v19.0"


class PosterAgent:
    """
    Publishes approved images to Instagram via the official Meta Graph API.
    """

    def __init__(self):
        self.config = get_config()
        self.flags = get_flags()
        self.access_token: Optional[str] = os.getenv("META_ACCESS_TOKEN")
        self.ig_account_id: Optional[str] = os.getenv("IG_ACCOUNT_ID")
        self.imgbb_api_key: Optional[str] = os.getenv("IMGBB_API_KEY")

    # ── Public entry point ────────────────────────────────────────────────────

    def post(self, image: dict, caption: str, topic: str = "", override_image_url: Optional[str] = None) -> Optional[str]:
        """
        Post an image to Instagram.

        Args:
            image:              Image dict from DB (must include 'local_path', 'id').
            caption:            Generated caption string.
            topic:              Trend topic used for this post (stored in posts table).
            override_image_url: Optional direct public URL to bypass local serving.

        Returns:
            Instagram post ID string on success, None on failure.
        """
        # Hot-reload flags
        self.flags = get_flags()
        self.config = get_config()

        # ── Checks ────────────────────────────────────────────────────────────
        if not self.flags.posting_enabled:
            logger.warning("posting_enabled=false — dry-run: post skipped")
            return None

        if not self.access_token or not self.ig_account_id:
            logger.error("META_ACCESS_TOKEN or IG_ACCOUNT_ID missing from .env")
            return None

        local_path: str = image.get("local_path", "")
        if not override_image_url:
            if not local_path or not os.path.isfile(local_path):
                logger.error(f"Image file not found: '{local_path}'")
                return None

        # ── Human approval gate ────────────────────────────────────────────────
        if self.flags.human_approval:
            if not self._request_human_approval(image, caption):
                logger.info("Human approval denied — post skipped")
                return None

        # ── Image server config ────────────────────────────────────────────────
        server_cfg = self.config.get("image_server", {})
        port: int = int(server_cfg.get("port", 8765))
        public_base: str = server_cfg.get("public_base_url", "").rstrip("/")
        use_ngrok: bool = server_cfg.get("use_ngrok", False)
        use_imgbb: bool = server_cfg.get("use_imgbb", False)

        if not use_imgbb and not use_ngrok and (not public_base or "YOUR_PUBLIC_IP" in public_base):
            logger.error(
                "image_server.public_base_url is not configured in config.yaml and use_ngrok/use_imgbb are false. "
                "Enable ImgBB, ngrok, or set it to your machine's public IP/domain."
            )
            return None

        serve_dir = os.path.dirname(local_path)
        filename = os.path.basename(local_path)

        # ── Start image server and publish ─────────────────────────────────────
        ig_post_id: Optional[str] = None
        cloud_public_id: Optional[str] = None

        try:
            if override_image_url:
                logger.info(f"Using external image URL to bypass local server: {override_image_url}")
                ig_post_id = self._publish(image_url=override_image_url, caption=caption)
            elif server_cfg.get("use_cloudinary", True):  # Default to True now
                logger.info("Using Cloudinary for image hosting (cloud native)...")
                image_url, cloud_public_id = upload_image(local_path)
                if not image_url:
                    logger.error("Cloudinary upload failed. Cannot post.")
                    return None
                ig_post_id = self._publish(image_url=image_url, caption=caption)
            elif use_imgbb:
                logger.info("Using ImgBB to connect image via public URL...")
                imgbb_url = self._upload_to_imgbb(local_path)
                if not imgbb_url:
                    logger.error("Failed to upload image to ImgBB.")
                    return None
                logger.info(f"Posting image {image.get('id')} | ImgBB URL: {imgbb_url}")
                ig_post_id = self._publish(image_url=imgbb_url, caption=caption)
            elif use_ngrok:
                logger.info("Using ngrok to tunnel local image server...")
                with TunneledImageServer(serve_dir=serve_dir, port=port) as tunnel_url:
                    image_url = f"{tunnel_url}/{filename}"
                    logger.info(f"Posting image {image.get('id')} | Tunnel URL: {image_url}")
                    time.sleep(2.0)  # Wait for tunnel to propagate fully
                    ig_post_id = self._publish(image_url=image_url, caption=caption)
            else:
                image_url = f"{public_base}:{port}/{filename}"
                logger.info(f"Posting image {image.get('id')} | Static URL: {image_url}")
                with ImageServer(serve_dir=serve_dir, port=port, public_base_url=public_base):
                    time.sleep(1.5)  # brief pause for server to be ready
                    ig_post_id = self._publish(image_url=image_url, caption=caption)
        except Exception as exc:
            logger.error(f"Post failed: {exc}", exc_info=True)
            return None

        # ── Cleanup ────────────────────────────────────────────────────────────
        if cloud_public_id:
            delete_image(cloud_public_id)

        # ── Persist result ─────────────────────────────────────────────────────
        if ig_post_id:
            image_id = image.get("id")
            is_repost = isinstance(image_id, str) and str(image_id).startswith("repost_")

            if override_image_url:
                logger.info(f"✅ Posted test URL! IG post ID: {ig_post_id} (Skipped DB save for test)")
            elif is_repost:
                # Repost images are tracked in repost_log table — skip images FK
                logger.info(f"✅ Repost published! IG post ID: {ig_post_id}")
                # Auto-clean local image file — no reason to keep it after publish
                cleanup_path = image.get("_cleanup_path") or image.get("local_path")
                if cleanup_path and os.path.exists(cleanup_path):
                    try:
                        os.remove(cleanup_path)
                        logger.info(f"Cleaned up local file: {os.path.basename(cleanup_path)}")
                    except Exception as e:
                        logger.warning(f"Could not delete local repost file: {e}")
            else:
                save_post(
                    image_id=image_id,
                    caption=caption,
                    topic=topic,
                    ig_post_id=ig_post_id,
                )
                mark_image_posted(image_id)
                logger.info(f"✅ Posted! IG post ID: {ig_post_id}")

        return ig_post_id

    # ── Meta Graph API flow ───────────────────────────────────────────────────

    @retry(
        max_attempts=3,
        backoff_factor=2,
        initial_wait=3.0,
        exceptions=(requests.RequestException,),
    )
    def _upload_to_imgbb(self, local_path: str) -> Optional[str]:
        """Uploads an image to ImgBB and returns the public URL."""
        if not self.imgbb_api_key:
            logger.error("IMGBB_API_KEY is not set in .env")
            return None

        import base64
        url = "https://api.imgbb.com/1/upload"
        try:
            with open(local_path, "rb") as f:
                img_data = f.read()

            payload = {
                "key": self.imgbb_api_key,
                "image": base64.b64encode(img_data).decode('utf-8')
            }
            logger.info("Uploading image to ImgBB...")
            res = requests.post(url, data=payload, timeout=30)
            res.raise_for_status()
            
            img_url = res.json().get("data", {}).get("url")
            return img_url
        except Exception as e:
            logger.error(f"ImgBB upload failed: {e}")
            return None

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
        data = {
            "image_url": image_url,
            "caption": caption,
            "access_token": self.access_token,
        }

        resp = requests.post(url, data=data, timeout=30)

        # Always log Meta's error body before acting — makes diagnosis instant
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
                logger.error(f"Full Meta error: {err_body}")

                # Known non-retryable error codes
                if code == 10:
                    logger.error(
                        "→ Fix: Check your access token scopes. "
                        "Needs: instagram_basic + instagram_content_publish"
                    )
                    return None

                if code == 190:
                    logger.critical(
                        "→ Fix: Token expired. Generate a new long-lived token "
                        "and update META_ACCESS_TOKEN in .env"
                    )
                    return None

            except Exception:
                logger.error(f"Meta API HTTP {resp.status_code}: {resp.text[:500]}")

        resp.raise_for_status()

        container_id = resp.json().get("id")
        logger.info(f"Container created: {container_id}")
        return container_id

    def _await_container(self, container_id: str, max_wait_secs: int = 90) -> bool:
        """
        Poll container status until FINISHED or ERROR.

        Polls every 5 seconds up to max_wait_secs.
        Returns True if FINISHED, False otherwise.
        """
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
                logger.debug(f"Container {container_id} status: {status}")

                if status == "FINISHED":
                    return True
                if status == "ERROR":
                    logger.error(f"Container {container_id} processing ERROR")
                    return False
            except requests.RequestException as exc:
                logger.warning(f"Container poll error: {exc}")

            time.sleep(5)
            waited += 5

        logger.warning(
            f"Container {container_id} not FINISHED after {max_wait_secs}s "
            f"(last status unknown) — giving up"
        )
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

    # ── Human approval ────────────────────────────────────────────────────────

    def _request_human_approval(self, image: dict, caption: str) -> bool:
        """
        Print a preview and wait for [y/n] input.
        Returns True if approved, False if denied or interrupted.
        """
        print("\n" + "=" * 65)
        print("🔔  HUMAN APPROVAL REQUIRED")
        print("=" * 65)
        print(f"  Image    : {os.path.basename(image.get('local_path', 'N/A'))}")
        print(f"  Size     : {image.get('width', '?')}×{image.get('height', '?')} px")
        print()
        print("  Caption preview:")
        preview = caption[:400]
        for line in preview.splitlines():
            print(f"    {line}")
        if len(caption) > 400:
            print("    ...")
        print("=" * 65)

        try:
            answer = input("  Post this? [y/N]: ").strip().lower()
            return answer == "y"
        except (EOFError, KeyboardInterrupt):
            return False
