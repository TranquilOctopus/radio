from __future__ import annotations

import shutil
import textwrap
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pkm.ingest.opml import OPMLEntry, parse_opml
from pkm.queue import FeedRow, Queue


# ---------------------------------------------------------------------------
# OPML parser
# ---------------------------------------------------------------------------


def _write_opml(tmp_path: Path, xml: str) -> Path:
    p = tmp_path / "feeds.opml"
    p.write_text(textwrap.dedent(xml).strip(), encoding="utf-8")
    return p


def test_opml_flat_apple_style(tmp_path: Path) -> None:
    """Flat body/outline layout returns entries with category=None."""
    opml = _write_opml(
        tmp_path,
        """\
        <?xml version="1.0" encoding="UTF-8"?>
        <opml version="1.0">
          <head><title>My Podcasts</title></head>
          <body>
            <outline text="Show A" xmlUrl="https://a.example.com/feed.xml" htmlUrl="https://a.example.com"/>
            <outline text="Show B" xmlUrl="https://b.example.com/feed.xml"/>
          </body>
        </opml>
        """,
    )
    entries = parse_opml(opml)
    assert len(entries) == 2
    assert all(e.category is None for e in entries)
    assert entries[0].feed_url == "https://a.example.com/feed.xml"
    assert entries[0].html_url == "https://a.example.com"
    assert entries[1].feed_url == "https://b.example.com/feed.xml"


def test_opml_nested_overcast_style(tmp_path: Path) -> None:
    """Nested category/outline layout propagates the category name."""
    opml = _write_opml(
        tmp_path,
        """\
        <?xml version="1.0" encoding="UTF-8"?>
        <opml version="1.0">
          <head/>
          <body>
            <outline text="Tech">
              <outline text="Show A" xmlUrl="https://a.example.com/feed.xml"/>
              <outline text="Show B" xmlUrl="https://b.example.com/feed.xml"/>
            </outline>
            <outline text="News">
              <outline text="Show C" xmlUrl="https://c.example.com/feed.xml"/>
            </outline>
          </body>
        </opml>
        """,
    )
    entries = parse_opml(opml)
    assert len(entries) == 3
    assert entries[0].category == "Tech"
    assert entries[1].category == "Tech"
    assert entries[2].category == "News"


def test_opml_skips_nodes_without_xmlurl(tmp_path: Path) -> None:
    """Category-folder outlines (no xmlUrl) are skipped at the top level."""
    opml = _write_opml(
        tmp_path,
        """\
        <?xml version="1.0" encoding="UTF-8"?>
        <opml version="1.0">
          <head/>
          <body>
            <outline text="Just a folder"/>
            <outline text="Show A" xmlUrl="https://a.example.com/feed.xml"/>
          </body>
        </opml>
        """,
    )
    entries = parse_opml(opml)
    assert len(entries) == 1
    assert entries[0].feed_url == "https://a.example.com/feed.xml"


def test_opml_missing_text_falls_back_to_feed_url(tmp_path: Path) -> None:
    """Entries missing text/title fall back to using the xmlUrl as the title."""
    opml = _write_opml(
        tmp_path,
        """\
        <?xml version="1.0" encoding="UTF-8"?>
        <opml version="1.0">
          <head/>
          <body>
            <outline xmlUrl="https://a.example.com/feed.xml"/>
          </body>
        </opml>
        """,
    )
    entries = parse_opml(opml)
    assert len(entries) == 1
    # When text/title are absent the entry is still returned; title falls back to feed_url.
    assert entries[0].feed_url == "https://a.example.com/feed.xml"
    assert entries[0].title == "https://a.example.com/feed.xml"


def test_opml_empty_body(tmp_path: Path) -> None:
    opml = _write_opml(
        tmp_path,
        """\
        <?xml version="1.0" encoding="UTF-8"?>
        <opml version="1.0">
          <head/>
          <body/>
        </opml>
        """,
    )
    assert parse_opml(opml) == []


# ---------------------------------------------------------------------------
# URL ingestor (mocked yt_dlp)
# ---------------------------------------------------------------------------


def test_fetch_audio_url_populates_fetchedaudio(tmp_path: Path) -> None:
    yt_dlp = pytest.importorskip("yt_dlp")

    fake_info = {
        "id": "abc123",
        "title": "Cool Video",
        "duration": 300,
        "uploader": "Test Channel",
        "upload_date": "20240115",
        "ext": "mp3",
    }

    # Write a placeholder file so audio_path.exists() can be asserted if needed.
    dest = tmp_path / "abc123.mp3"
    dest.write_bytes(b"\x00")

    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = lambda s: s
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = fake_info

    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl_instance):
        from pkm.ingest.url import fetch_audio_url

        result = fetch_audio_url("https://youtube.com/watch?v=abc123", tmp_path)

    assert result.title == "Cool Video"
    assert result.duration_s == 300
    assert result.uploader == "Test Channel"
    assert result.upload_date == "2024-01-15"
    assert result.source_url == "https://youtube.com/watch?v=abc123"
    assert result.audio_path == tmp_path / "abc123.mp3"


def test_fetch_audio_url_playlist_takes_first_entry(tmp_path: Path) -> None:
    pytest.importorskip("yt_dlp")

    entry = {
        "id": "ep1",
        "title": "Episode 1",
        "duration": 60,
        "uploader": None,
        "upload_date": None,
        "ext": "mp3",
    }
    fake_info = {"entries": [entry, {"id": "ep2", "title": "Episode 2"}]}

    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = lambda s: s
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = fake_info

    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl_instance):
        from pkm.ingest.url import fetch_audio_url

        result = fetch_audio_url("https://youtube.com/playlist?list=X", tmp_path)

    assert result.title == "Episode 1"


def test_fetch_audio_url_raises_fetcherror_on_download_error(tmp_path: Path) -> None:
    yt_dlp = pytest.importorskip("yt_dlp")
    from yt_dlp.utils import DownloadError

    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = lambda s: s
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.side_effect = DownloadError("404")

    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl_instance):
        from pkm.ingest.url import FetchError, fetch_audio_url

        with pytest.raises(FetchError):
            fetch_audio_url("https://example.com/bad", tmp_path)


# ---------------------------------------------------------------------------
# Inbox watcher
# ---------------------------------------------------------------------------


@pytest.fixture()
def inbox_queue(tmp_path: Path) -> Queue:
    q = Queue(tmp_path / "jobs.db")
    q.init_schema()
    yield q
    q.close()


def test_inbox_watcher_enqueues_dropped_file(tmp_path: Path, inbox_queue: Queue) -> None:
    from pkm.ingest.inbox import InboxWatcher

    watch_dir = tmp_path / "inbox"
    watch_dir.mkdir()

    watcher = InboxWatcher(watch_dir, inbox_queue, settle_seconds=0.1)
    watcher.start()
    try:
        mp3 = watch_dir / "my_episode.mp3"
        mp3.write_bytes(b"\xff\xfb" + b"\x00" * 512)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if inbox_queue.jobs_by_status("PENDING"):
                break
            time.sleep(0.2)
    finally:
        watcher.stop()

    jobs = inbox_queue.jobs_by_status("PENDING")
    assert len(jobs) == 1
    assert jobs[0].episode_title == "my_episode"
    assert "file://" in jobs[0].episode_url


def test_inbox_watcher_creates_synthetic_feed(tmp_path: Path, inbox_queue: Queue) -> None:
    from pkm.ingest.inbox import InboxWatcher

    watch_dir = tmp_path / "inbox2"
    watch_dir.mkdir()

    watcher = InboxWatcher(watch_dir, inbox_queue, settle_seconds=0.1)
    watcher.start()
    try:
        mp3 = watch_dir / "track.mp3"
        mp3.write_bytes(b"\xff\xfb" + b"\x00" * 512)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if inbox_queue.jobs_by_status("PENDING"):
                break
            time.sleep(0.2)
    finally:
        watcher.stop()

    feed = inbox_queue.get_feed_by_slug("inbox")
    assert feed is not None
    assert feed.title == "Inbox"


def test_inbox_watcher_ignores_non_audio(tmp_path: Path, inbox_queue: Queue) -> None:
    from pkm.ingest.inbox import InboxWatcher

    watch_dir = tmp_path / "inbox3"
    watch_dir.mkdir()

    # Pre-create a non-audio file before start so the startup scan sees it.
    (watch_dir / "readme.txt").write_text("ignore me")

    watcher = InboxWatcher(watch_dir, inbox_queue, settle_seconds=0.1)
    watcher.start()
    try:
        time.sleep(0.5)
    finally:
        watcher.stop()

    assert inbox_queue.jobs_by_status("PENDING") == []


def test_inbox_watcher_picks_up_existing_files(tmp_path: Path, inbox_queue: Queue) -> None:
    from pkm.ingest.inbox import InboxWatcher

    watch_dir = tmp_path / "inbox4"
    watch_dir.mkdir()
    # File already present before watcher starts.
    mp3 = watch_dir / "pre_existing.mp3"
    mp3.write_bytes(b"\xff\xfb" + b"\x00" * 512)

    watcher = InboxWatcher(watch_dir, inbox_queue, settle_seconds=0.1)
    watcher.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if inbox_queue.jobs_by_status("PENDING"):
                break
            time.sleep(0.2)
    finally:
        watcher.stop()

    jobs = inbox_queue.jobs_by_status("PENDING")
    assert any("pre_existing" in j.episode_title for j in jobs)


# ---------------------------------------------------------------------------
# Pipeline file:// short-circuit
# ---------------------------------------------------------------------------


def test_pipeline_download_file_url(tmp_path: Path) -> None:
    """_download copies a local file:// URL into audio_dir without HTTP."""
    from pkm.config import Config, PathsConfig
    from pkm.pipeline import Pipeline
    from pkm.queue import FeedRow, JobRow, Queue
    from pkm.store.graph import Graph
    from pkm.store.vault import Vault

    # Prepare a real local audio file.
    src = tmp_path / "source.mp3"
    src.write_bytes(b"\xff\xfb" + b"\x00" * 128)

    config = Config(
        paths=PathsConfig(
            audio_dir=str(tmp_path / "audio"),
            transcripts_dir=str(tmp_path / "transcripts"),
            graph_dir=str(tmp_path / "graph.kuzu"),
            db_path=str(tmp_path / "jobs.db"),
            vault_dir=str(tmp_path / "vault"),
        )
    )

    q = Queue(Path(config.paths.db_path))
    q.init_schema()

    feed_id = q.upsert_feed(
        FeedRow(feed_url="inbox://local", title="Inbox", podcast_slug="inbox", style="informational")
    )
    job = JobRow(
        feed_id=feed_id,
        episode_guid="test-guid",
        episode_title="source",
        episode_url=f"file://{src}",
        status="PENDING",
    )
    job_id = q.enqueue_job(job)
    job.id = job_id

    g = Graph(Path(config.paths.graph_dir))
    g.init_schema()

    # Use a downloader that always fails so we verify the file:// path never calls it.
    def _failing_downloader(url: str, dest: Path) -> Path:
        raise AssertionError("HTTP downloader called for a file:// URL")

    pipeline = Pipeline(
        config=config,
        queue=q,
        graph=g,
        vault=Vault(Path(config.paths.vault_dir)),
        downloader=_failing_downloader,
    )

    # Fetch just the job row with id set.
    db_job = q.jobs_by_status("PENDING")[0]
    pipeline._download(db_job)

    # File must have been copied to audio_dir.
    dest = Path(config.paths.audio_dir) / f"{feed_id}_source.mp3"
    assert dest.exists()
    assert dest.read_bytes()[:2] == b"\xff\xfb"

    g.close()
    q.close()
