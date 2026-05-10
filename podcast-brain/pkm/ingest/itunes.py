from __future__ import annotations

import re
from typing import Optional

import httpx
from pydantic import BaseModel

_ITUNES_BASE = "https://itunes.apple.com"

# Matches both show URLs (id1234567890) and episode share URLs (id1234567890?i=...)
_APPLE_URL_RE = re.compile(r"/id(\d+)")


class ItunesPodcast(BaseModel):
    collection_id: int
    collection_name: str
    feed_url: str
    artist_name: Optional[str] = None
    language: Optional[str] = None


def _default_client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0))


def _parse_result(item: dict) -> ItunesPodcast | None:
    feed_url = item.get("feedUrl")
    if not feed_url:
        return None
    return ItunesPodcast(
        collection_id=item["collectionId"],
        collection_name=item.get("collectionName", ""),
        feed_url=feed_url,
        artist_name=item.get("artistName") or None,
        language=item.get("primaryGenreName") or None,
    )


def search_podcast(
    term: str, limit: int = 10, *, client: httpx.Client | None = None
) -> list[ItunesPodcast]:
    c = client or _default_client()
    resp = c.get(
        f"{_ITUNES_BASE}/search",
        params={"term": term, "entity": "podcast", "limit": limit},
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for item in data.get("results", []):
        p = _parse_result(item)
        if p:
            results.append(p)
    return results


def lookup_by_id(itunes_id: int, *, client: httpx.Client | None = None) -> ItunesPodcast | None:
    c = client or _default_client()
    resp = c.get(
        f"{_ITUNES_BASE}/lookup",
        params={"id": itunes_id, "entity": "podcast"},
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results", [])
    if not results:
        return None
    return _parse_result(results[0])


def resolve_apple_podcasts_url(
    url: str, *, client: httpx.Client | None = None
) -> ItunesPodcast | None:
    """
    Parses an Apple Podcasts share URL and resolves it via iTunes Lookup.
    Handles both show URLs (podcasts.apple.com/.../id1234567890) and episode
    share URLs (same path with ?i=<episode_id>; the show id is still the path segment).
    Returns None if the URL doesn't match the expected pattern.
    """
    m = _APPLE_URL_RE.search(url)
    if not m:
        return None
    itunes_id = int(m.group(1))
    return lookup_by_id(itunes_id, client=client)
