from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel
from slugify import slugify


class FeedRow(BaseModel):
    id: Optional[int] = None
    feed_url: str
    podcast_index_id: Optional[int] = None
    itunes_id: Optional[int] = None
    title: str
    podcast_slug: str
    style: str = "informational"
    language: Optional[str] = None
    added_at: Optional[str] = None
    last_polled_at: Optional[str] = None


class JobRow(BaseModel):
    id: Optional[int] = None
    feed_id: int
    episode_guid: str
    episode_title: str
    episode_url: str
    episode_published: Optional[str] = None
    episode_duration_s: Optional[int] = None
    status: str = "PENDING"
    priority: int = 100
    attempts: int = 0
    last_error: Optional[str] = None
    enqueued_at: Optional[str] = None
    updated_at: Optional[str] = None


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS feeds (
  id INTEGER PRIMARY KEY,
  feed_url TEXT NOT NULL UNIQUE,
  podcast_index_id INTEGER,
  itunes_id INTEGER,
  title TEXT NOT NULL,
  podcast_slug TEXT NOT NULL,
  style TEXT NOT NULL DEFAULT 'informational',
  language TEXT,
  added_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_polled_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
  episode_guid TEXT NOT NULL,
  episode_title TEXT NOT NULL,
  episode_url TEXT NOT NULL,
  episode_published TEXT,
  episode_duration_s INTEGER,
  status TEXT NOT NULL DEFAULT 'PENDING',
  priority INTEGER NOT NULL DEFAULT 100,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  enqueued_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (feed_id, episode_guid)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_priority ON jobs(status, priority);

CREATE TABLE IF NOT EXISTS claude_calls (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0
);
"""

_VALID_STYLES = {"informational", "banter", "narrative", "skip"}


def _make_slug(title: str) -> str:
    return slugify(title, max_length=60)


def _row_to_feed(row: sqlite3.Row) -> FeedRow:
    return FeedRow(
        id=row["id"],
        feed_url=row["feed_url"],
        podcast_index_id=row["podcast_index_id"],
        itunes_id=row["itunes_id"],
        title=row["title"],
        podcast_slug=row["podcast_slug"],
        style=row["style"],
        language=row["language"],
        added_at=row["added_at"],
        last_polled_at=row["last_polled_at"],
    )


def _row_to_job(row: sqlite3.Row) -> JobRow:
    return JobRow(
        id=row["id"],
        feed_id=row["feed_id"],
        episode_guid=row["episode_guid"],
        episode_title=row["episode_title"],
        episode_url=row["episode_url"],
        episode_published=row["episode_published"],
        episode_duration_s=row["episode_duration_s"],
        status=row["status"],
        priority=row["priority"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        enqueued_at=row["enqueued_at"],
        updated_at=row["updated_at"],
    )


class Queue:
    """
    SQLite-backed job queue.  Not multi-writer-safe — designed for a single
    daemon process.  WAL mode allows concurrent readers (e.g. the FastAPI
    status dashboard) while the daemon writes.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def upsert_feed(self, feed: FeedRow) -> int:
        slug = feed.podcast_slug or _make_slug(feed.title)
        cur = self._conn.execute(
            """
            INSERT INTO feeds (feed_url, podcast_index_id, itunes_id, title, podcast_slug,
                               style, language)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(feed_url) DO UPDATE SET
                podcast_index_id = excluded.podcast_index_id,
                itunes_id        = excluded.itunes_id,
                title            = excluded.title,
                podcast_slug     = excluded.podcast_slug,
                style            = excluded.style,
                language         = excluded.language
            RETURNING id
            """,
            (
                feed.feed_url,
                feed.podcast_index_id,
                feed.itunes_id,
                feed.title,
                slug,
                feed.style,
                feed.language,
            ),
        )
        row = cur.fetchone()
        self._conn.commit()
        return row[0]

    def get_feed_by_url(self, url: str) -> FeedRow | None:
        cur = self._conn.execute("SELECT * FROM feeds WHERE feed_url = ?", (url,))
        row = cur.fetchone()
        return _row_to_feed(row) if row else None

    def get_feed_by_slug(self, slug: str) -> FeedRow | None:
        cur = self._conn.execute("SELECT * FROM feeds WHERE podcast_slug = ?", (slug,))
        row = cur.fetchone()
        return _row_to_feed(row) if row else None

    def list_feeds(self) -> list[FeedRow]:
        cur = self._conn.execute("SELECT * FROM feeds ORDER BY added_at")
        return [_row_to_feed(r) for r in cur.fetchall()]

    def set_feed_style(self, slug_or_id: str | int, style: str) -> None:
        if style not in _VALID_STYLES:
            raise ValueError(f"style must be one of {_VALID_STYLES}")
        if isinstance(slug_or_id, int):
            self._conn.execute("UPDATE feeds SET style = ? WHERE id = ?", (style, slug_or_id))
        else:
            self._conn.execute("UPDATE feeds SET style = ? WHERE podcast_slug = ?", (style, slug_or_id))
        self._conn.commit()

    def enqueue_job(self, job: JobRow) -> int | None:
        """Returns the new job id, or None if deduped (same feed_id + episode_guid)."""
        try:
            cur = self._conn.execute(
                """
                INSERT INTO jobs (feed_id, episode_guid, episode_title, episode_url,
                                  episode_published, episode_duration_s, status, priority)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    job.feed_id,
                    job.episode_guid,
                    job.episode_title,
                    job.episode_url,
                    job.episode_published,
                    job.episode_duration_s,
                    job.status,
                    job.priority,
                ),
            )
            row = cur.fetchone()
            self._conn.commit()
            return row[0]
        except sqlite3.IntegrityError:
            # UNIQUE(feed_id, episode_guid) violation — dedupe
            return None

    def claim_next_job(self, statuses: list[str]) -> JobRow | None:
        placeholders = ",".join("?" * len(statuses))
        cur = self._conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY priority ASC, enqueued_at ASC LIMIT 1",
            statuses,
        )
        row = cur.fetchone()
        return _row_to_job(row) if row else None

    def update_job_status(self, job_id: int, status: str, error: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE jobs SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (status, error, now, job_id),
        )
        self._conn.commit()

    def jobs_by_status(self, status: str, limit: int = 100) -> list[JobRow]:
        cur = self._conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY priority ASC, enqueued_at ASC LIMIT ?",
            (status, limit),
        )
        return [_row_to_job(r) for r in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Queue":
        return self

    def __exit__(self, *_) -> None:
        self.close()
