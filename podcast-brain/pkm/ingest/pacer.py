from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

from pkm.config import BacklogConfig
from pkm.ingest.podcastindex import EpisodeInfo
from pkm.queue import JobRow, Queue

# Two-tier priority status: PENDING-BACKLOG = held by pacer; PENDING = released to pipeline.
BACKLOG_STATUS = "PENDING-BACKLOG"
RELEASED_STATUS = "PENDING"


def _episode_priority(ep: EpisodeInfo, strategy: str, idx: int) -> int:
    if strategy == "oldest_first":
        if ep.published_at is None:
            return 9_000_000_000
        return int(ep.published_at.timestamp())
    if strategy == "newest_first":
        if ep.published_at is None:
            return 0
        return -int(ep.published_at.timestamp())
    # interleaved: enqueue order, used as tie-breaker; daemon will round-robin during release
    return idx


class Pacer:
    def __init__(self, queue: Queue, config: BacklogConfig) -> None:
        self._queue = queue
        self._config = config

    def enqueue_episodes_for_feed(
        self,
        feed_id: int,
        episodes: Iterable[EpisodeInfo],
        strategy: str | None = None,
    ) -> int:
        strat = strategy or self._config.strategy
        new_count = 0
        for idx, ep in enumerate(episodes):
            job = JobRow(
                feed_id=feed_id,
                episode_guid=ep.guid,
                episode_title=ep.title,
                episode_url=ep.enclosure_url,
                episode_published=ep.published_at.isoformat() if ep.published_at else None,
                episode_duration_s=ep.duration_s,
                status=BACKLOG_STATUS,
                priority=_episode_priority(ep, strat, idx),
            )
            if self._queue.enqueue_job(job) is not None:
                new_count += 1
        return new_count

    def daily_release_count(self, now: datetime | None = None) -> int:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=1)
        cur = self._queue._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status != ? AND updated_at >= ?",
            (BACKLOG_STATUS, cutoff.isoformat()),
        )
        return int(cur.fetchone()[0])

    def _released_per_feed_today(self, now: datetime | None = None) -> dict[int, int]:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=1)
        cur = self._queue._conn.execute(
            """
            SELECT feed_id, COUNT(*) FROM jobs
            WHERE status != ? AND updated_at >= ?
            GROUP BY feed_id
            """,
            (BACKLOG_STATUS, cutoff.isoformat()),
        )
        return {row[0]: int(row[1]) for row in cur.fetchall()}

    def release_next_batch(self, now: datetime | None = None) -> list[int]:
        already = self.daily_release_count(now=now)
        budget = max(0, self._config.max_episodes_per_day - already)
        if budget == 0:
            return []

        per_feed_used = self._released_per_feed_today(now=now)
        per_feed_cap = self._config.per_show_daily_cap

        cur = self._queue._conn.execute(
            "SELECT id, feed_id FROM jobs WHERE status = ? ORDER BY priority ASC, enqueued_at ASC",
            (BACKLOG_STATUS,),
        )
        candidates = cur.fetchall()

        # Bucket candidates by feed in the order they were returned (already priority-sorted).
        by_feed: dict[int, list[int]] = defaultdict(list)
        feed_order: list[int] = []
        for row in candidates:
            fid = row["feed_id"]
            if fid not in by_feed:
                feed_order.append(fid)
            by_feed[fid].append(row["id"])

        # Round-robin across feeds, respecting per-feed daily cap.
        promoted: list[int] = []
        feed_take: dict[int, int] = defaultdict(int)
        i = 0
        while len(promoted) < budget and feed_order:
            fid = feed_order[i % len(feed_order)]
            cap_remaining = per_feed_cap - per_feed_used.get(fid, 0) - feed_take[fid]
            if cap_remaining > 0 and by_feed[fid]:
                job_id = by_feed[fid].pop(0)
                promoted.append(job_id)
                feed_take[fid] += 1
                if not by_feed[fid] or per_feed_cap - per_feed_used.get(fid, 0) - feed_take[fid] <= 0:
                    feed_order.remove(fid)
                    continue
            else:
                feed_order.remove(fid)
                continue
            i += 1

        if not promoted:
            return []

        now_iso = (now or datetime.now(timezone.utc)).isoformat()
        placeholders = ",".join("?" * len(promoted))
        self._queue._conn.execute(
            f"UPDATE jobs SET status = ?, updated_at = ? WHERE id IN ({placeholders})",
            (RELEASED_STATUS, now_iso, *promoted),
        )
        self._queue._conn.commit()
        return promoted
