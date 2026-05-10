from __future__ import annotations

from pathlib import Path

import pytest

from pkm.queue import FeedRow, JobRow, Queue


@pytest.fixture
def queue(tmp_path: Path) -> Queue:
    q = Queue(tmp_path / "jobs.db")
    q.init_schema()
    return q


def _make_feed(url: str = "https://example.com/feed.xml", **overrides) -> FeedRow:
    base = dict(
        feed_url=url,
        title="Example Show",
        podcast_slug="example-show",
        style="informational",
    )
    base.update(overrides)
    return FeedRow(**base)


def test_init_schema_idempotent(queue: Queue) -> None:
    queue.init_schema()
    queue.init_schema()


def test_upsert_feed_dedupes_by_url(queue: Queue) -> None:
    fid1 = queue.upsert_feed(_make_feed(title="First Title"))
    fid2 = queue.upsert_feed(_make_feed(title="Updated Title", style="banter"))
    assert fid1 == fid2
    feed = queue.get_feed_by_url("https://example.com/feed.xml")
    assert feed is not None
    assert feed.title == "Updated Title"
    assert feed.style == "banter"


def test_get_feed_by_slug(queue: Queue) -> None:
    queue.upsert_feed(_make_feed())
    feed = queue.get_feed_by_slug("example-show")
    assert feed is not None
    assert feed.feed_url == "https://example.com/feed.xml"


def test_set_feed_style_validates(queue: Queue) -> None:
    queue.upsert_feed(_make_feed())
    queue.set_feed_style("example-show", "banter")
    assert queue.get_feed_by_slug("example-show").style == "banter"
    with pytest.raises(ValueError):
        queue.set_feed_style("example-show", "nonsense")


def test_enqueue_dedupes_by_guid(queue: Queue) -> None:
    fid = queue.upsert_feed(_make_feed())
    j1 = queue.enqueue_job(JobRow(feed_id=fid, episode_guid="g1", episode_title="A", episode_url="u1"))
    j2 = queue.enqueue_job(JobRow(feed_id=fid, episode_guid="g2", episode_title="B", episode_url="u2"))
    j3 = queue.enqueue_job(JobRow(feed_id=fid, episode_guid="g1", episode_title="A-dup", episode_url="u1"))
    assert j1 is not None and j2 is not None
    assert j3 is None
    assert len(queue.jobs_by_status("PENDING")) == 2


def test_claim_next_job_priority_order(queue: Queue) -> None:
    fid = queue.upsert_feed(_make_feed())
    queue.enqueue_job(JobRow(feed_id=fid, episode_guid="g1", episode_title="low-prio", episode_url="u1", priority=200))
    queue.enqueue_job(JobRow(feed_id=fid, episode_guid="g2", episode_title="hi-prio", episode_url="u2", priority=50))
    nxt = queue.claim_next_job(["PENDING"])
    assert nxt is not None
    assert nxt.episode_title == "hi-prio"


def test_update_job_status(queue: Queue) -> None:
    fid = queue.upsert_feed(_make_feed())
    job_id = queue.enqueue_job(JobRow(feed_id=fid, episode_guid="g1", episode_title="A", episode_url="u1"))
    queue.update_job_status(job_id, "DOWNLOADED")
    assert queue.jobs_by_status("PENDING") == []
    downloaded = queue.jobs_by_status("DOWNLOADED")
    assert len(downloaded) == 1
    assert downloaded[0].status == "DOWNLOADED"
