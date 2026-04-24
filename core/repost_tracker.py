"""
core/repost_tracker.py — Lightweight dedup tracker using a plain text file.

Stores one Instagram shortcode (post ID) per line in data/reposted_ids.txt.
Replaces the SQLite repost_log for cloud-friendly, zero-dependency tracking.

Usage:
    from core.repost_tracker import is_reposted, mark_reposted

    if not is_reposted("DU3i6qPDTOH"):
        # process and post...
        mark_reposted("DU3i6qPDTOH")
"""

import os
from pathlib import Path

_TRACKER_FILE = Path("data/reposted_ids.txt")


def _ensure_file() -> None:
    _TRACKER_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not _TRACKER_FILE.exists():
        _TRACKER_FILE.touch()


def is_reposted(post_id: str) -> bool:
    """Return True if this shortcode has already been posted."""
    _ensure_file()
    ids = _TRACKER_FILE.read_text(encoding="utf-8").splitlines()
    return post_id.strip() in ids


def mark_reposted(post_id: str) -> None:
    """Append a shortcode to the tracker file."""
    _ensure_file()
    with open(_TRACKER_FILE, "a", encoding="utf-8") as f:
        f.write(post_id.strip() + "\n")


def all_reposted() -> list[str]:
    """Return all tracked post IDs (for debugging/inspection)."""
    _ensure_file()
    return [l.strip() for l in _TRACKER_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
