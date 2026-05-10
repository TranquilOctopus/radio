from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from pydantic import BaseModel

from pkm.ingest.podcastindex import EpisodeInfo


class FeedFetchResult(BaseModel):
    status: int  # 200, 304, or error code
    title: Optional[str] = None
    language: Optional[str] = None
    episodes: list[EpisodeInfo] = []
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    error: Optional[str] = None


def _parse_duration(raw: str | None) -> int | None:
    """
    Parses itunes:duration which can be "HH:MM:SS", "MM:SS", or plain seconds "3661".
    Returns total seconds as int, or None if unparseable.
    """
    if not raw:
        return None
    raw = raw.strip()
    if ":" in raw:
        parts = raw.split(":")
        try:
            parts_int = [int(p) for p in parts]
        except ValueError:
            return None
        if len(parts_int) == 3:
            return parts_int[0] * 3600 + parts_int[1] * 60 + parts_int[2]
        if len(parts_int) == 2:
            return parts_int[0] * 60 + parts_int[1]
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _entry_to_episode(entry: dict) -> EpisodeInfo | None:
    """Returns None if no audio enclosure is found."""
    enclosures = entry.get("enclosures", [])
    audio_url = None
    for enc in enclosures:
        mime = enc.get("type", "")
        if mime.startswith("audio/"):
            audio_url = enc.get("href") or enc.get("url")
            break

    if not audio_url:
        return None

    guid = entry.get("id") or entry.get("guid")
    if not guid:
        # Stable deterministic fallback from enclosure URL
        guid = "enc-" + hashlib.sha1(audio_url.encode()).hexdigest()[:16]

    published_at = None
    parsed = entry.get("published_parsed")
    if parsed:
        try:
            published_at = datetime(*parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

    duration_raw = None
    itunes = entry.get("itunes_duration")
    if itunes:
        duration_raw = str(itunes)

    return EpisodeInfo(
        guid=str(guid),
        title=entry.get("title", ""),
        enclosure_url=audio_url,
        published_at=published_at,
        duration_s=_parse_duration(duration_raw),
    )


def fetch_feed(
    feed_url: str,
    *,
    client: httpx.Client | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FeedFetchResult:
    """
    Fetches an RSS feed via httpx (so the client is injectable for tests),
    then parses with feedparser.  Supports conditional GET to skip unchanged feeds.
    """
    c = client or httpx.Client(
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
    )
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        resp = c.get(feed_url, headers=headers, follow_redirects=True)
    except httpx.HTTPError as exc:
        return FeedFetchResult(status=0, error=str(exc))

    if resp.status_code == 304:
        return FeedFetchResult(
            status=304,
            etag=resp.headers.get("ETag"),
            last_modified=resp.headers.get("Last-Modified"),
        )

    if resp.status_code != 200:
        return FeedFetchResult(status=resp.status_code, error=f"HTTP {resp.status_code}")

    import feedparser  # lazy: avoid hard dep at import time so missing feedparser only breaks fetch_feed
    parsed = feedparser.parse(resp.content)

    feed_meta = parsed.get("feed", {})
    title = feed_meta.get("title") or None
    language = feed_meta.get("language") or None

    episodes: list[EpisodeInfo] = []
    for entry in parsed.get("entries", []):
        ep = _entry_to_episode(entry)
        if ep is not None:
            episodes.append(ep)

    return FeedFetchResult(
        status=200,
        title=title,
        language=language,
        episodes=episodes,
        etag=resp.headers.get("ETag"),
        last_modified=resp.headers.get("Last-Modified"),
    )
