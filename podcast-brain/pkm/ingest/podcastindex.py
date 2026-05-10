"""
PodcastIndex API client.

PodcastIndex has no anonymous tier — api_key + api_secret are required.
Get free credentials at https://podcastindex.org/signup.

Auth: HMAC-SHA1.  Each request sends:
  X-Auth-Key: <api_key>
  X-Auth-Date: <unix_timestamp_str>
  Authorization: sha1(<api_key> + <api_secret> + <unix_timestamp_str>)
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from pydantic import BaseModel

from pkm.config import PodcastIndexConfig


class PodcastSearchResult(BaseModel):
    feed_id: int
    itunes_id: Optional[int] = None
    title: str
    feed_url: str
    description: Optional[str] = None
    language: Optional[str] = None


class PodcastInfo(BaseModel):
    feed_id: int
    itunes_id: Optional[int] = None
    title: str
    feed_url: str
    description: Optional[str] = None
    language: Optional[str] = None


class EpisodeInfo(BaseModel):
    guid: str
    title: str
    enclosure_url: str
    published_at: Optional[datetime] = None
    duration_s: Optional[int] = None


_BASE_URL = "https://api.podcastindex.org/api/1.0"


def _parse_episode(ep: dict) -> EpisodeInfo:
    published_at = None
    ts = ep.get("datePublished")
    if ts:
        try:
            published_at = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except (ValueError, TypeError):
            pass

    return EpisodeInfo(
        guid=ep.get("guid") or ep.get("id", ""),
        title=ep.get("title", ""),
        enclosure_url=ep.get("enclosureUrl", ""),
        published_at=published_at,
        duration_s=ep.get("duration") or None,
    )


def _parse_feed(feed: dict) -> PodcastInfo:
    return PodcastInfo(
        feed_id=feed["id"],
        itunes_id=feed.get("itunesId") or None,
        title=feed.get("title", ""),
        feed_url=feed.get("url", ""),
        description=feed.get("description") or None,
        language=feed.get("language") or None,
    )


class PodcastIndex:
    def __init__(self, config: PodcastIndexConfig, client: httpx.Client | None = None) -> None:
        if not config.api_key or not config.api_secret:
            raise ValueError(
                "PodcastIndex api_key and api_secret are required. "
                "Get free credentials at https://podcastindex.org/signup"
            )
        self._config = config
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
        )

    def _auth_headers(self) -> dict[str, str]:
        auth_date = str(int(time.time()))
        token = self._config.api_key + self._config.api_secret + auth_date
        auth_hash = hashlib.sha1(token.encode("utf-8")).hexdigest()
        return {
            "User-Agent": self._config.user_agent,
            "X-Auth-Date": auth_date,
            "X-Auth-Key": self._config.api_key,
            "Authorization": auth_hash,
        }

    def _get(self, path: str, params: dict) -> dict:
        resp = self._client.get(
            f"{_BASE_URL}{path}",
            params=params,
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def search_by_term(self, term: str, max_results: int = 10) -> list[PodcastSearchResult]:
        data = self._get("/search/byterm", {"q": term, "max": max_results})
        feeds = data.get("feeds", [])
        results = []
        for f in feeds:
            results.append(
                PodcastSearchResult(
                    feed_id=f["id"],
                    itunes_id=f.get("itunesId") or None,
                    title=f.get("title", ""),
                    feed_url=f.get("url", ""),
                    description=f.get("description") or None,
                    language=f.get("language") or None,
                )
            )
        return results

    def lookup_by_feed_url(self, url: str) -> PodcastInfo | None:
        data = self._get("/podcasts/byfeedurl", {"url": url})
        feed = data.get("feed")
        if not feed:
            return None
        return _parse_feed(feed)

    def lookup_by_itunes_id(self, itunes_id: int) -> PodcastInfo | None:
        data = self._get("/podcasts/byitunesid", {"id": itunes_id})
        feed = data.get("feed")
        if not feed:
            return None
        return _parse_feed(feed)

    def episodes_by_feed_id(
        self, feed_id: int, max_results: int = 1000, since: int | None = None
    ) -> list[EpisodeInfo]:
        params: dict = {"id": feed_id, "max": max_results}
        if since is not None:
            params["since"] = since
        data = self._get("/episodes/byfeedid", params)
        return [_parse_episode(ep) for ep in data.get("items", [])]

    def episodes_by_feed_url(self, feed_url: str, max_results: int = 1000) -> list[EpisodeInfo]:
        data = self._get("/episodes/byfeedurl", {"url": feed_url, "max": max_results})
        return [_parse_episode(ep) for ep in data.get("items", [])]
