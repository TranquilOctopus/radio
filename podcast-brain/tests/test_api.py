from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from pkm.api import create_app
from pkm.budget import BudgetTracker
from pkm.queue import FeedRow, JobRow, Queue


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Returns (config_path, db_path) for a hermetic test."""
    db_path = tmp_path / "data" / "jobs.db"
    config_text = f"""
[paths]
db_path = "{db_path}"
audio_dir = "{tmp_path / 'data' / 'audio'}"
transcripts_dir = "{tmp_path / 'data' / 'transcripts'}"
graph_dir = "{tmp_path / 'data' / 'graph.kuzu'}"
vault_dir = "{tmp_path / 'vault'}"

[budget]
monthly_cap_usd = 20.0
warn_at_pct = 80
summarize_model = "claude-sonnet-4-6"
"""
    config_path = tmp_path / "config.toml"
    config_path.write_text(config_text)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return config_path, db_path


@pytest.fixture
def client(tmp_workspace: tuple[Path, Path]) -> TestClient:
    config_path, _ = tmp_workspace
    app = create_app(config_path=config_path)
    return TestClient(app)


def _seed_feed(db_path: Path, **overrides) -> int:
    base = dict(
        feed_url="https://example.com/feed.xml",
        title="Example Show",
        podcast_slug="example-show",
        style="informational",
    )
    base.update(overrides)
    with Queue(db_path) as q:
        q.init_schema()
        return q.upsert_feed(FeedRow(**base))


def _seed_job(db_path: Path, feed_id: int, **overrides) -> int:
    base = dict(
        feed_id=feed_id, episode_guid="g1",
        episode_title="Ep 1", episode_url="https://example.com/ep1.mp3",
    )
    base.update(overrides)
    with Queue(db_path) as q:
        q.init_schema()
        return q.enqueue_job(JobRow(**base))


def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_dashboard_html(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "podcast-brain" in r.text
    # Basic smoke: critical UI sections present
    assert "Recent jobs" in r.text
    assert "Submit URL" in r.text


def test_queue_summary_empty(client: TestClient) -> None:
    r = client.get("/api/queue/summary")
    assert r.status_code == 200
    assert r.json() == {"counts": {}, "total": 0}


def test_queue_summary_counts_by_status(client: TestClient, tmp_workspace) -> None:
    _, db_path = tmp_workspace
    fid = _seed_feed(db_path)
    _seed_job(db_path, fid, episode_guid="g1")
    _seed_job(db_path, fid, episode_guid="g2")
    with Queue(db_path) as q:
        j3 = q.enqueue_job(JobRow(feed_id=fid, episode_guid="g3",
                                   episode_title="x", episode_url="x"))
        q.update_job_status(j3, "DONE")

    r = client.get("/api/queue/summary")
    body = r.json()
    assert body["total"] == 3
    assert body["counts"]["PENDING"] == 2
    assert body["counts"]["DONE"] == 1


def test_queue_jobs_filter_by_status(client: TestClient, tmp_workspace) -> None:
    _, db_path = tmp_workspace
    fid = _seed_feed(db_path)
    _seed_job(db_path, fid, episode_guid="p1")
    with Queue(db_path) as q:
        d = q.enqueue_job(JobRow(feed_id=fid, episode_guid="d1",
                                  episode_title="d", episode_url="x"))
        q.update_job_status(d, "DONE")

    r = client.get("/api/queue/jobs?status=PENDING")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "PENDING"


def test_feeds_list(client: TestClient, tmp_workspace) -> None:
    _, db_path = tmp_workspace
    _seed_feed(db_path, podcast_slug="show-a", title="Show A",
               feed_url="https://a.example.com/rss")
    _seed_feed(db_path, podcast_slug="show-b", title="Show B",
               feed_url="https://b.example.com/rss", style="banter")
    r = client.get("/api/feeds")
    assert r.status_code == 200
    feeds = r.json()
    assert len(feeds) == 2
    slugs = {f["podcast_slug"] for f in feeds}
    assert slugs == {"show-a", "show-b"}


def test_feed_set_style(client: TestClient, tmp_workspace) -> None:
    _, db_path = tmp_workspace
    fid = _seed_feed(db_path)

    r = client.post(f"/api/feeds/{fid}/style", json={"style": "banter"})
    assert r.status_code == 200
    assert r.json()["style"] == "banter"

    feeds = client.get("/api/feeds").json()
    assert feeds[0]["style"] == "banter"


def test_feed_set_style_rejects_invalid(client: TestClient, tmp_workspace) -> None:
    _, db_path = tmp_workspace
    fid = _seed_feed(db_path)
    r = client.post(f"/api/feeds/{fid}/style", json={"style": "garbage"})
    assert r.status_code == 422  # pydantic validation rejects unknown literal


def test_budget_view(client: TestClient, tmp_workspace) -> None:
    _, db_path = tmp_workspace
    with BudgetTracker(db_path) as t:
        usage = MagicMock(input_tokens=1000, output_tokens=500,
                          cache_read_input_tokens=200,
                          cache_creation_input_tokens=0)
        t.record("claude-sonnet-4-6", usage, cost_usd=0.05)

    r = client.get("/api/budget")
    assert r.status_code == 200
    body = r.json()
    assert body["mtd_spend_usd"] == pytest.approx(0.05)
    assert body["monthly_cap_usd"] == pytest.approx(20.0)
    assert body["pct_used"] == pytest.approx(0.25)
    assert len(body["by_model"]) == 1


def test_submit_file(client: TestClient, tmp_workspace, tmp_path) -> None:
    _, db_path = tmp_workspace
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"\x00" * 100)

    r = client.post("/api/submit/file", json={"path": str(audio_file)})
    assert r.status_code == 200
    body = r.json()
    assert body["queued"] is True

    with Queue(db_path) as q:
        feeds = [f for f in q.list_feeds() if f.podcast_slug == "inbox"]
        assert len(feeds) == 1
        jobs = q.jobs_by_status("PENDING")
        assert len(jobs) == 1
        assert jobs[0].episode_url.startswith("file://")


def test_submit_file_rejects_missing(client: TestClient) -> None:
    r = client.post("/api/submit/file", json={"path": "/no/such/file.mp3"})
    assert r.status_code == 400


def test_submit_url_enqueues_and_calls_background(client: TestClient, tmp_workspace) -> None:
    _, db_path = tmp_workspace

    fake_audio = MagicMock(
        title="Mocked title",
        audio_path=Path("/tmp/mocked.mp3"),
        duration_s=120,
        source_url="https://youtube.com/watch?v=xyz",
        uploader="someone",
        upload_date="2026-05-01",
    )
    with patch("pkm.ingest.url.fetch_audio_url", return_value=fake_audio) as mock_fetch:
        r = client.post(
            "/api/submit/url",
            json={"url": "https://youtube.com/watch?v=xyz", "style": "informational"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["queued"] is True
        assert "job_id" in body
        # TestClient runs background tasks synchronously after the response.
        mock_fetch.assert_called_once()

    with Queue(db_path) as q:
        feeds = [f for f in q.list_feeds() if f.podcast_slug == "url-imports"]
        assert len(feeds) == 1
        # After background fetch, status is PENDING and url is file://
        cur = q._conn.execute("SELECT status, episode_url FROM jobs WHERE id = ?",
                              (body["job_id"],))
        row = cur.fetchone()
        assert row[0] == "PENDING"
        assert row[1].startswith("file://")


def test_feed_add_via_api_with_rss_url(client: TestClient, tmp_workspace) -> None:
    _, db_path = tmp_workspace
    with patch("pkm.ingest.subscribe.PodcastIndex"):
        # Without PI credentials configured (default), we skip the lookup —
        # so PodcastIndex isn't actually called; the patch is just defensive.
        r = client.post(
            "/api/feeds",
            json={"url": "https://example.com/feed.xml", "style": "informational"},
        )
    assert r.status_code == 200
    body = r.json()
    assert "feed_id" in body
    assert body["slug"]

    with Queue(db_path) as q:
        feeds = q.list_feeds()
        assert any(f.feed_url == "https://example.com/feed.xml" for f in feeds)


def test_feed_add_rejects_show_name(client: TestClient) -> None:
    r = client.post("/api/feeds", json={"url": "Lex Fridman"})
    assert r.status_code == 400


def test_feed_add_resolves_apple_url(client: TestClient, tmp_workspace) -> None:
    fake = MagicMock(feed_url="https://example.com/resolved.xml")
    with patch("pkm.ingest.subscribe.resolve_apple_podcasts_url", return_value=fake):
        r = client.post(
            "/api/feeds",
            json={"url": "https://podcasts.apple.com/us/podcast/foo/id1234567890"},
        )
    assert r.status_code == 200
    assert r.json()["feed_url"] == "https://example.com/resolved.xml"


def test_feed_search_returns_candidates(client: TestClient) -> None:
    fake_results = [
        MagicMock(collection_id=111, collection_name="Show One",
                  artist_name="A", feed_url="https://a.example/rss"),
        MagicMock(collection_id=222, collection_name="Show Two",
                  artist_name="B", feed_url="https://b.example/rss"),
    ]
    with patch("pkm.ingest.itunes.search_podcast", return_value=fake_results):
        r = client.get("/api/feeds/search?term=test&limit=2")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0]["itunes_id"] == 111
    assert body[1]["name"] == "Show Two"


def test_digest_latest_returns_404_when_empty(client: TestClient) -> None:
    r = client.get("/api/digest/latest")
    assert r.status_code == 404


def test_digest_latest_returns_most_recent(client: TestClient, tmp_workspace, tmp_path) -> None:
    # The vault dir is under tmp_path per the fixture; create digest files there.
    digests = tmp_path / "vault" / "digests" / "weekly"
    digests.mkdir(parents=True)
    (digests / "2026-W17.md").write_text("# Week 17\nOlder digest")
    (digests / "2026-W18.md").write_text("# Week 18\nNewer digest")

    r = client.get("/api/digest/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["week"] == "2026-W18"
    assert "Newer digest" in body["markdown"]


def test_submit_url_dedupes(client: TestClient, tmp_workspace) -> None:
    fake_audio = MagicMock(
        title="t", audio_path=Path("/tmp/x.mp3"), duration_s=10,
        source_url="https://a.example.com/x", uploader=None, upload_date=None,
    )
    with patch("pkm.ingest.url.fetch_audio_url", return_value=fake_audio):
        first = client.post("/api/submit/url",
                            json={"url": "https://a.example.com/x"}).json()
        second = client.post("/api/submit/url",
                             json={"url": "https://a.example.com/x"}).json()
    assert first["queued"] is True
    assert second["queued"] is False
    assert second["reason"] == "duplicate"
