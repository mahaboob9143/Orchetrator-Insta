"""
core/db.py — SQLite schema definition and query helpers for InstaAgent.

All database access goes through this module.
Uses context managers to ensure commit/rollback on every call.

Tables:
  images          — media downloaded from Unsplash
  posts           — published Instagram posts
  engagement      — performance snapshots at 1hr / 6hr / 24hr
  trends          — trending topics from Pytrends + competitor scraping
  posting_windows — learned optimal posting hours (updated from engagement data)
  repost_log      — tracks scraped + reposted content from source accounts
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger("DB")

DB_PATH = "instaagent.db"

_SCHEMA = """
-- ── Images ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS images (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT    NOT NULL,
    hash            TEXT    NOT NULL UNIQUE,        -- perceptual hash (phash)
    source          TEXT    DEFAULT 'unsplash',
    width           INTEGER,
    height          INTEGER,
    local_path      TEXT,
    downloaded_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    status          TEXT    DEFAULT 'queued'        -- queued | posted | rejected
);

-- ── Posts ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id        INTEGER REFERENCES images(id),
    caption         TEXT,
    topic           TEXT,
    posted_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    ig_post_id      TEXT    UNIQUE,
    status          TEXT    DEFAULT 'published'     -- published | failed
);

-- ── Engagement snapshots ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS engagement (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id                 INTEGER REFERENCES posts(id),
    checked_at              DATETIME DEFAULT CURRENT_TIMESTAMP,
    check_interval_hours    INTEGER,
    likes                   INTEGER DEFAULT 0,
    comments                INTEGER DEFAULT 0,
    shares                  INTEGER DEFAULT 0,
    reach                   INTEGER DEFAULT 0,
    saves                   INTEGER DEFAULT 0,
    is_high_performer       INTEGER DEFAULT 0       -- 0 = normal, 1 = high
);

-- ── Trends ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trends (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    topic           TEXT    NOT NULL,
    score           REAL,
    source          TEXT,                           -- pytrends | competitor | insights
    captured_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Posting windows (machine-learned from engagement data) ───────────────────
CREATE TABLE IF NOT EXISTS posting_windows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hour            INTEGER UNIQUE,                 -- 0-23
    avg_reach       REAL,
    sample_size     INTEGER DEFAULT 1,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
-- ── Repost log ────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS repost_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_username   TEXT    NOT NULL,
    source_post_id    TEXT    NOT NULL UNIQUE,  -- instagrapi media.pk (dedup key)
    source_shortcode  TEXT,                     -- e.g. CxABC123 (for reference)
    local_image_path  TEXT,
    original_caption  TEXT,
    rewritten_caption TEXT,
    ig_post_id        TEXT,                     -- our published post ID
    reposted_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


# ─── Connection ───────────────────────────────────────────────────────────────

@contextmanager
def get_conn():
    """
    Context manager that yields a committed sqlite3 connection.
    Rolls back and re-raises on any exception.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # allow concurrent reads during writes
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str = DB_PATH):
    """Create all tables if they don't exist. Safe to call on every startup."""
    import os
    global DB_PATH
    DB_PATH = db_path
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) if os.path.dirname(db_path) else ".", exist_ok=True)
    os.makedirs("media/queue", exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    with get_conn() as conn:
        conn.executescript(_SCHEMA)

    logger.info(f"Database ready at '{db_path}'")


# ─── Image queries ────────────────────────────────────────────────────────────

def image_hash_exists(phash: str) -> bool:
    """Return True if this perceptual hash is already in the DB."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM images WHERE hash = ?", (phash,)
        ).fetchone()
        return row is not None


def get_all_image_hashes() -> List[str]:
    """Return all stored perceptual hashes for deduplication."""
    with get_conn() as conn:
        rows = conn.execute("SELECT hash FROM images").fetchall()
        return [r["hash"] for r in rows]


def save_image(
    url: str,
    phash: str,
    width: int,
    height: int,
    local_path: str,
    source: str = "unsplash",
) -> int:
    """Insert a new image record. Returns the new row id."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO images (url, hash, source, width, height, local_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (url, phash, source, width, height, local_path),
        )
        return cur.lastrowid


def get_queued_images() -> List[Dict[str, Any]]:
    """Return all images with status='queued', oldest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM images WHERE status = 'queued' ORDER BY downloaded_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_queued_image_count() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM images WHERE status = 'queued'"
        ).fetchone()
        return row["cnt"]


def mark_image_posted(image_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE images SET status = 'posted' WHERE id = ?", (image_id,)
        )


def mark_image_rejected(image_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE images SET status = 'rejected' WHERE id = ?", (image_id,)
        )


# ─── Post queries ─────────────────────────────────────────────────────────────

def save_post(image_id: int, caption: str, topic: str, ig_post_id: str) -> int:
    """Insert a published post record. Returns the new row id."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO posts (image_id, caption, topic, ig_post_id) VALUES (?, ?, ?, ?)",
            (image_id, caption, topic, ig_post_id),
        )
        return cur.lastrowid


def posts_today_count() -> int:
    """Return number of successfully published posts today (UTC)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM posts "
            "WHERE date(posted_at) = date('now') AND status = 'published'"
        ).fetchone()
        return row["cnt"]


def get_recent_posts(limit: int = 20) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM posts ORDER BY posted_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Engagement queries ───────────────────────────────────────────────────────

def save_engagement(
    post_id: int,
    interval_hours: int,
    likes: int,
    comments: int,
    shares: int,
    reach: int,
    saves: int,
    is_high_performer: bool = False,
):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO engagement
               (post_id, check_interval_hours, likes, comments, shares, reach, saves, is_high_performer)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (post_id, interval_hours, likes, comments, shares, reach, saves, int(is_high_performer)),
        )


def get_engagement_for_post(post_id: int) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM engagement WHERE post_id = ? ORDER BY checked_at ASC",
            (post_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_top_hashtags_by_reach(limit: int = 25) -> List[str]:
    """
    Extract hashtags from captions of high-performing posts,
    ranked by how often they appear (proxy for effectiveness).
    Falls back to empty list if not enough data yet.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT p.caption, MAX(e.reach) as max_reach
               FROM posts p
               JOIN engagement e ON e.post_id = p.id
               WHERE e.is_high_performer = 1
               GROUP BY p.id
               ORDER BY max_reach DESC
               LIMIT 10"""
        ).fetchall()

    hashtag_counts: Dict[str, int] = {}
    for row in rows:
        caption = row["caption"] or ""
        for word in caption.split():
            if word.startswith("#") and len(word) > 1:
                hashtag_counts[word] = hashtag_counts.get(word, 0) + 1

    sorted_tags = sorted(hashtag_counts.items(), key=lambda x: x[1], reverse=True)
    return [tag for tag, _ in sorted_tags[:limit]]


# ─── Trend queries ────────────────────────────────────────────────────────────

def save_trend(topic: str, score: float, source: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO trends (topic, score, source) VALUES (?, ?, ?)",
            (topic, score, source),
        )


def get_top_trends(n: int = 5) -> List[Dict[str, Any]]:
    """Return top N trending topics averaged over the last 7 days."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT topic, AVG(score) as avg_score
               FROM trends
               WHERE captured_at > datetime('now', '-7 days')
               GROUP BY topic
               ORDER BY avg_score DESC
               LIMIT ?""",
            (n,),
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Posting window queries ───────────────────────────────────────────────────

def get_best_posting_hours(n: int = 3) -> List[int]:
    """
    Return the top N hours (0-23) with highest average reach.
    Only returns hours with at least 2 data points (sample_size >= 2).
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT hour FROM posting_windows
               WHERE sample_size >= 2
               ORDER BY avg_reach DESC
               LIMIT ?""",
            (n,),
        ).fetchall()
        return [r["hour"] for r in rows]


def upsert_posting_window(hour: int, reach: float):
    """Update the running average reach for a given posting hour."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, avg_reach, sample_size FROM posting_windows WHERE hour = ?",
            (hour,),
        ).fetchone()

        if existing:
            new_avg = (existing["avg_reach"] * existing["sample_size"] + reach) / (
                existing["sample_size"] + 1
            )
            conn.execute(
                "UPDATE posting_windows "
                "SET avg_reach = ?, sample_size = sample_size + 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE hour = ?",
                (new_avg, hour),
            )
        else:
            conn.execute(
                "INSERT INTO posting_windows (hour, avg_reach) VALUES (?, ?)",
                (hour, reach),
            )


# ─── Repost log queries ───────────────────────────────────────────────────────

def is_post_reposted(source_post_id: str) -> bool:
    """Return True if this source post ID has already been reposted."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM repost_log WHERE source_post_id = ?",
            (str(source_post_id),),
        ).fetchone()
        return row is not None


def log_repost(
    source_username: str,
    source_post_id: str,
    source_shortcode: str,
    local_image_path: str,
    original_caption: str,
    rewritten_caption: str,
) -> int:
    """Insert a new repost log entry. Returns the new row id."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO repost_log
               (source_username, source_post_id, source_shortcode,
                local_image_path, original_caption, rewritten_caption)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (source_username, str(source_post_id), source_shortcode,
             local_image_path, original_caption, rewritten_caption),
        )
        return cur.lastrowid


def update_repost_ig_id(source_post_id: str, ig_post_id: str):
    """Update the published Instagram post ID after a successful repost."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE repost_log SET ig_post_id = ? WHERE source_post_id = ?",
            (ig_post_id, str(source_post_id)),
        )
