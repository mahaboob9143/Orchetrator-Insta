"""
core/cloudinary_uploader.py — Upload images to Cloudinary and return a public URL.

Used by PosterAgent for repost images:
  - Replaces the ngrok tunnel flow entirely
  - Works both locally and in cloud (GitHub Actions / Railway / Render)
  - Auto-deletes from Cloudinary after Meta publishes the post

Credentials read from environment:
  CLOUDINARY_CLOUD_NAME
  CLOUDINARY_API_KEY
  CLOUDINARY_API_SECRET
"""

import os
from typing import Optional

from core.logger import get_logger

logger = get_logger("Cloudinary")


def _configured() -> bool:
    return all([
        os.getenv("CLOUDINARY_CLOUD_NAME"),
        os.getenv("CLOUDINARY_API_KEY"),
        os.getenv("CLOUDINARY_API_SECRET"),
    ])


def upload_image(local_path: str, folder: str = "instaagent/reposts") -> tuple[Optional[str], Optional[str]]:
    """
    Upload a local image file to Cloudinary.

    Returns:
        (public_url, public_id) on success
        (None, None) on failure

    public_id is needed later to delete the image from Cloudinary.
    """
    if not _configured():
        logger.error(
            "Cloudinary credentials missing. Set CLOUDINARY_CLOUD_NAME, "
            "CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET in .env"
        )
        return None, None

    try:
        import cloudinary
        import cloudinary.uploader

        cloudinary.config(
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
            api_key=os.getenv("CLOUDINARY_API_KEY"),
            api_secret=os.getenv("CLOUDINARY_API_SECRET"),
            secure=True,
        )

        logger.info(f"Uploading {os.path.basename(local_path)} to Cloudinary...")
        result = cloudinary.uploader.upload(
            local_path,
            folder=folder,
            resource_type="image",
        )

        public_url = result.get("secure_url")
        public_id = result.get("public_id")

        logger.info(f"Cloudinary upload OK → {public_url}")
        return public_url, public_id

    except Exception as e:
        logger.error(f"Cloudinary upload failed: {e}")
        return None, None


def delete_image(public_id: str) -> None:
    """
    Delete an image from Cloudinary by its public_id.
    Called after Meta successfully publishes to avoid storage accumulation.
    """
    if not public_id:
        return

    try:
        import cloudinary
        import cloudinary.uploader

        cloudinary.config(
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
            api_key=os.getenv("CLOUDINARY_API_KEY"),
            api_secret=os.getenv("CLOUDINARY_API_SECRET"),
        )

        cloudinary.uploader.destroy(public_id)
        logger.info(f"Cloudinary cleanup OK: {public_id}")

    except Exception as e:
        logger.warning(f"Cloudinary cleanup failed (non-critical): {e}")
