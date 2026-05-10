from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from pkm.config import BacklogConfig, PodcastIndexConfig
from pkm.ingest.itunes import lookup_by_id, resolve_apple_podcasts_url, search_podcast
from pkm.ingest.pacer import BACKLOG_STATUS, RELEASED_STATUS, Pacer
from pkm.ingest.podcastindex import EpisodeInfo, PodcastIndex
from pkm.ingest.rss import _parse_duration, fetch_feed
from pkm.queue import FeedRow, Queue


# ---------- PodcastIndex auth + parsing ----------


def test_podcastindex_requires_credentials() -> None:
    with pytest.raises(ValueError):
        PodcastIndex(PodcastIndexConfig(api_key="", api_secret=""))


def test_podcastindex_byfeedurl_parses_response() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "status": "true",
                "feed": {
                    "id": 920666,
                    "itunesId": 1234567890,
                    "title": "Show Title",
                    "url": "https://example.com/feed.xml",
                    "description": "desc",
                    "language": "en",
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    pi = PodcastIndex(
        PodcastIndexConfig(api_key="testkey", api_secret="testsecret"), client=client
    )
    info = pi.lookup_by_feed_url("https://example.com/feed.xml")
    assert info is not None
    assert info.feed_id == 920666
    assert info.itunes_id == 1234567890
    assert info.title == "Show Title"
    # Auth headers present and well-formed
    h = captured["headers"]
    assert h["x-auth-key"] == "testkey"
    assert "x-auth-date" in h
    # SHA1(key + secret + date) hex digest is 40 chars
    assert len(h["authorization"]) == 40
    assert int(h["x-auth-date"]) > 0


def test_podcastindex_episodes_parses_dates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "true",
                "items": [
                    {
                        "guid": "ep-1",
                        "title": "Episode One",
                        "enclosureUrl": "https://example.com/ep1.mp3",
                        "datePublished": 1700000000,
                        "duration": 3661,
                    },
                    {
                        "guid": "ep-2",
                        "title": "Episode Two",
                        "enclosureUrl": "https://example.com/ep2.mp3",
                    },
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    pi = PodcastIndex(PodcastIndexConfig(api_key="k", api_secret="s"), client=client)
    eps = pi.episodes_by_feed_id(123)
    assert len(eps) == 2
    assert eps[0].guid == "ep-1"
    assert eps[0].duration_s == 3661
    assert eps[0].published_at is not None
    assert eps[0].published_at.year == 2023
    assert eps[1].published_at is None


# ---------- iTunes Search/Lookup + URL resolution ----------


def _itunes_lookup_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "resultCount": 1,
            "results": [
                {
                    "collectionId": 1234567890,
                    "collectionName": "Some Show",
                    "feedUrl": "https://example.com/feed.xml",
                    "artistName": "Some Artist",
                    "primaryGenreName": "News",
                }
            ],
        },
    )


def test_itunes_search_returns_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "search" in str(request.url)
        assert "term=foo" in str(request.url)
        return _itunes_lookup_response()

    client = httpx.Client(transport=httpx.MockTransport(handler))
    results = search_podcast("foo", client=client)
    assert len(results) == 1
    assert results[0].collection_id == 1234567890


def test_itunes_lookup_by_id() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: _itunes_lookup_response()))
    p = lookup_by_id(1234567890, client=client)
    assert p is not None
    assert p.feed_url == "https://example.com/feed.xml"


@pytest.mark.parametrize(
    "url",
    [
        "https://podcasts.apple.com/us/podcast/foo/id1234567890",
        "https://podcasts.apple.com/se/podcast/foo/id1234567890?i=1000600000",
        "https://podcasts.apple.com/gb/podcast/some-show/id1234567890",
    ],
)
def test_resolve_apple_podcasts_url(url: str) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return _itunes_lookup_response()

    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = resolve_apple_podcasts_url(url, client=client)
    assert p is not None
    assert captured["params"]["id"] == "1234567890"


def test_resolve_apple_podcasts_url_returns_none_for_garbage() -> None:
    p = resolve_apple_podcasts_url("https://example.com/not-apple")
    assert p is None


# ---------- RSS parsing ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("3661", 3661),
        ("01:01:01", 3661),
        ("61:01", 3661),
        ("notanumber", None),
        (None, None),
        ("", None),
    ],
)
def test_parse_duration(raw, expected) -> None:
    assert _parse_duration(raw) == expected


_RSS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel>
  <title>Test Show</title>
  <language>en-us</language>
  <item>
    <title>Episode One</title>
    <guid>ep1-guid</guid>
    <pubDate>Mon, 15 Jan 2024 10:00:00 +0000</pubDate>
    <itunes:duration>01:00:00</itunes:duration>
    <enclosure url="https://example.com/ep1.mp3" type="audio/mpeg" length="100"/>
  </item>
  <item>
    <title>Episode Two (no audio)</title>
    <guid>ep2-guid</guid>
    <enclosure url="https://example.com/ep2.pdf" type="application/pdf" length="100"/>
  </item>
</channel>
</rss>
"""


def test_rss_fetch_feed_parses_audio_only() -> None:
    pytest.importorskip("feedparser")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_RSS_XML, headers={"ETag": "abc"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = fetch_feed("https://example.com/feed.xml", client=client)
    assert result.status == 200
    assert result.title == "Test Show"
    assert result.language == "en-us"
    assert result.etag == "abc"
    # Only the audio enclosure entry is kept
    assert len(result.episodes) == 1
    assert result.episodes[0].guid == "ep1-guid"
    assert result.episodes[0].duration_s == 3600


def test_rss_fetch_handles_304() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("if-none-match") == "abc"
        return httpx.Response(304)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = fetch_feed("https://example.com/feed.xml", client=client, etag="abc")
    assert result.status == 304
    assert result.episodes == []


# ---------- Pacer ----------


@pytest.fixture
def queue_with_feeds(tmp_path: Path) -> tuple[Queue, list[int]]:
    q = Queue(tmp_path / "jobs.db")
    q.init_schema()
    fids = []
    for i in range(3):
        fids.append(
            q.upsert_feed(
                FeedRow(
                    feed_url=f"https://example.com/feed{i}.xml",
                    title=f"Show {i}",
                    podcast_slug=f"show-{i}",
                )
            )
        )
    return q, fids


def _make_episodes(prefix: str, count: int, base_year: int = 2020) -> list[EpisodeInfo]:
    return [
        EpisodeInfo(
            guid=f"{prefix}-{i}",
            title=f"{prefix} ep {i}",
            enclosure_url=f"https://example.com/{prefix}-{i}.mp3",
            published_at=datetime(base_year + i, 1, 1, tzinfo=timezone.utc),
        )
        for i in range(count)
    ]


def test_pacer_enqueue_dedupes(queue_with_feeds) -> None:
    q, fids = queue_with_feeds
    p = Pacer(q, BacklogConfig(max_episodes_per_day=10, per_show_daily_cap=5))
    eps = _make_episodes("a", 3)
    assert p.enqueue_episodes_for_feed(fids[0], eps) == 3
    # Re-enqueue the same episodes — all dedupe
    assert p.enqueue_episodes_for_feed(fids[0], eps) == 0


def test_pacer_release_respects_global_and_per_show_cap(queue_with_feeds) -> None:
    q, fids = queue_with_feeds
    p = Pacer(q, BacklogConfig(max_episodes_per_day=5, per_show_daily_cap=2))
    p.enqueue_episodes_for_feed(fids[0], _make_episodes("a", 4))
    p.enqueue_episodes_for_feed(fids[1], _make_episodes("b", 4))
    p.enqueue_episodes_for_feed(fids[2], _make_episodes("c", 4))

    promoted = p.release_next_batch()
    assert len(promoted) == 5

    released = q.jobs_by_status(RELEASED_STATUS)
    by_feed = {fid: 0 for fid in fids}
    for job in released:
        by_feed[job.feed_id] += 1
    # Each feed should be capped at 2; total = 5 means at least one feed has 1
    for fid in fids:
        assert by_feed[fid] <= 2
    assert sum(by_feed.values()) == 5


def test_pacer_oldest_first_priority(queue_with_feeds) -> None:
    q, fids = queue_with_feeds
    p = Pacer(q, BacklogConfig(max_episodes_per_day=2, per_show_daily_cap=2, strategy="oldest_first"))
    # Episodes for feed[0] from 2020, 2021, 2022 — oldest_first should release 2020 + 2021 first.
    p.enqueue_episodes_for_feed(fids[0], _make_episodes("a", 3, base_year=2020))
    p.release_next_batch()
    released = q.jobs_by_status(RELEASED_STATUS)
    titles = sorted(j.episode_title for j in released)
    assert titles == ["a ep 0", "a ep 1"]


def test_pacer_daily_release_count_caps_subsequent_calls(queue_with_feeds) -> None:
    q, fids = queue_with_feeds
    p = Pacer(q, BacklogConfig(max_episodes_per_day=3, per_show_daily_cap=5))
    p.enqueue_episodes_for_feed(fids[0], _make_episodes("a", 10))

    first = p.release_next_batch()
    assert len(first) == 3
    second = p.release_next_batch()
    assert second == []  # daily budget exhausted

    # Simulate "next day" by fast-forwarding the clock past 24h
    future = datetime.now(timezone.utc) + timedelta(days=1, minutes=1)
    third = p.release_next_batch(now=future)
    assert len(third) == 3
