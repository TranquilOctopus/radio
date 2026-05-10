from __future__ import annotations

from pathlib import Path

from slugify import slugify

from pkm.config import Config, load_config
from pkm.ingest.itunes import resolve_apple_podcasts_url
from pkm.ingest.podcastindex import PodcastIndex
from pkm.queue import FeedRow, Queue

VALID_STYLES = ("informational", "banter", "narrative", "skip")
AUTO_STYLE_SENTINEL = "__pending_classification"


def add_feed_from_url(
    feed_url: str,
    *,
    requested_style: str | None = None,
    auto_style: bool = False,
    config: Config | None = None,
    config_path: Path | None = None,
) -> tuple[int, str, str]:
    """Resolve a feed URL via PodcastIndex (when credentials are present),
    upsert into the queue's feeds table, and return (feed_id, slug, style)."""
    if config is None:
        config = load_config(config_path)
    style = (
        AUTO_STYLE_SENTINEL if auto_style
        else (requested_style or "informational")
    )

    pi_id: int | None = None
    itunes_id: int | None = None
    title = feed_url
    language: str | None = None

    if config.ingest.podcastindex.api_key and config.ingest.podcastindex.api_secret:
        pi = PodcastIndex(config.ingest.podcastindex)
        info = pi.lookup_by_feed_url(feed_url)
        if info:
            pi_id = info.feed_id
            itunes_id = info.itunes_id
            title = info.title or title
            language = info.language

    slug = slugify(title, max_length=60)
    with Queue(Path(config.paths.db_path)) as q:
        q.init_schema()
        feed_id = q.upsert_feed(
            FeedRow(
                feed_url=feed_url,
                podcast_index_id=pi_id,
                itunes_id=itunes_id,
                title=title,
                podcast_slug=slug,
                style=style,
                language=language,
            )
        )
    return feed_id, slug, style


def resolve_input_to_feed_url(
    user_input: str,
    *,
    config: Config | None = None,
    config_path: Path | None = None,
) -> str | None:
    """Map a free-form input (RSS URL, Apple Podcasts URL) to an RSS feed URL.

    Returns None for show-name inputs — those need an interactive search step
    (search_podcast → user picks). Use that path explicitly.
    """
    if user_input.startswith("http") and "podcasts.apple.com" in user_input:
        info = resolve_apple_podcasts_url(user_input)
        return info.feed_url if info else None
    if user_input.startswith("http"):
        return user_input
    return None
