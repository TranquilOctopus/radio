from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(name="podcast-brain", help="Personal podcast knowledge system.")
feed_app = typer.Typer(help="Manage podcast feed subscriptions.")
ingest_app = typer.Typer(help="Run the ingestion pipeline.")

app.add_typer(feed_app, name="feed")
app.add_typer(ingest_app, name="ingest")

_NOT_YET = "not yet implemented"


# ---------------------------------------------------------------------------
# feed subcommands
# ---------------------------------------------------------------------------


_VALID_STYLES = ["informational", "banter", "narrative", "skip"]
_AUTO_STYLE_SENTINEL = "__pending_classification"


def _add_feed_from_url(
    feed_url: str,
    *,
    requested_style: str | None,
    auto_style: bool,
    config_path: Path | None = None,
) -> tuple[int, str, str]:
    from slugify import slugify

    from pkm.config import load_config
    from pkm.ingest.podcastindex import PodcastIndex
    from pkm.queue import FeedRow, Queue

    config = load_config(config_path)
    style = (
        _AUTO_STYLE_SENTINEL if auto_style
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


@feed_app.command("add")
def feed_add(
    url_or_name: str = typer.Argument(..., help="RSS URL, show name, or Apple Podcasts URL"),
    style: str | None = typer.Option(None, "--style", "-s", help=f"Show style: {' | '.join(_VALID_STYLES)}"),
    auto_style: bool = typer.Option(False, "--auto-style", help="Auto-detect show style on first ingest"),
) -> None:
    """Add a podcast feed by RSS URL, show name, or Apple Podcasts URL."""
    if style is not None and style not in _VALID_STYLES:
        typer.echo(f"Invalid style: {style}. Must be one of {_VALID_STYLES}.")
        raise typer.Exit(2)

    if url_or_name.startswith("http") and "podcasts.apple.com" in url_or_name:
        from pkm.ingest.itunes import resolve_apple_podcasts_url

        info = resolve_apple_podcasts_url(url_or_name)
        if info is None:
            typer.echo("Could not resolve Apple Podcasts URL to a feed.")
            raise typer.Exit(1)
        feed_url = info.feed_url
    elif url_or_name.startswith("http"):
        feed_url = url_or_name
    else:
        from pkm.ingest.itunes import search_podcast

        candidates = search_podcast(url_or_name, limit=3)
        if not candidates:
            typer.echo(f"No podcasts found matching '{url_or_name}'.")
            raise typer.Exit(1)
        for i, c in enumerate(candidates, 1):
            typer.echo(f"  {i}. {c.collection_name} ({c.artist_name or 'unknown'}) — {c.feed_url}")
        choice = typer.prompt("Pick a number (or 0 to cancel)", type=int)
        if choice == 0 or choice > len(candidates):
            raise typer.Exit(1)
        feed_url = candidates[choice - 1].feed_url

    feed_id, slug, final_style = _add_feed_from_url(
        feed_url, requested_style=style, auto_style=auto_style
    )
    typer.echo(f"Added: {feed_url} (slug: {slug}) feed_id={feed_id} style={final_style}")


@feed_app.command("style")
def feed_style(
    show: str = typer.Argument(..., help="Show slug or feed id"),
    style: str = typer.Argument(..., help=f"Show style: {' | '.join(_VALID_STYLES)}"),
) -> None:
    """Set the processing style for a subscribed show."""
    if style not in _VALID_STYLES:
        typer.echo(f"Invalid style: {style}. Must be one of {_VALID_STYLES}.")
        raise typer.Exit(2)

    from pkm.config import load_config
    from pkm.queue import Queue

    config = load_config()
    with Queue(Path(config.paths.db_path)) as q:
        q.init_schema()
        target: str | int = int(show) if show.isdigit() else show
        q.set_feed_style(target, style)
    typer.echo(f"Updated {show} → style={style}")


@feed_app.command("import")
def feed_import(
    opml_file: Path = typer.Argument(..., help="Path to OPML export file", exists=True, dir_okay=False),
) -> None:
    """Bulk-import subscriptions from an OPML file."""
    import xml.etree.ElementTree as ET

    tree = ET.parse(opml_file)
    feed_urls = [
        outline.get("xmlUrl")
        for outline in tree.iter("outline")
        if outline.get("xmlUrl")
    ]
    if not feed_urls:
        typer.echo("No <outline xmlUrl='…'> entries found.")
        raise typer.Exit(1)

    added = 0
    failed = 0
    for url in feed_urls:
        try:
            feed_id, slug, _ = _add_feed_from_url(url, requested_style=None, auto_style=True)
            typer.echo(f"  + {url} → {slug} (id={feed_id})")
            added += 1
        except Exception as exc:
            typer.echo(f"  ! {url}: {exc}")
            failed += 1
    typer.echo(f"OPML import: {added} added, {failed} failed.")


@feed_app.command("backfill")
def feed_backfill(
    show: str = typer.Argument(..., help="Show slug or feed id"),
    from_year: str | None = typer.Option(None, "--from", metavar="YYYY", help="Only episodes from this year onwards"),
) -> None:
    """Pull full episode history for a show from PodcastIndex and enqueue it."""
    from pkm.config import load_config
    from pkm.ingest.pacer import Pacer
    from pkm.ingest.podcastindex import PodcastIndex
    from pkm.queue import Queue

    config = load_config()
    if not (config.ingest.podcastindex.api_key and config.ingest.podcastindex.api_secret):
        typer.echo(
            "PodcastIndex credentials not configured. Set [ingest.podcastindex] "
            "api_key + api_secret in config.toml."
        )
        raise typer.Exit(1)

    with Queue(Path(config.paths.db_path)) as q:
        q.init_schema()
        feed = q.get_feed_by_slug(show) if not show.isdigit() else None
        if feed is None and show.isdigit():
            feeds = [f for f in q.list_feeds() if f.id == int(show)]
            feed = feeds[0] if feeds else None
        if feed is None or feed.podcast_index_id is None:
            typer.echo(f"No feed '{show}' or PodcastIndex ID missing — try `feed add` first.")
            raise typer.Exit(1)

        pi = PodcastIndex(config.ingest.podcastindex)
        since = None
        if from_year:
            from datetime import datetime, timezone
            since = int(datetime(int(from_year), 1, 1, tzinfo=timezone.utc).timestamp())

        episodes = pi.episodes_by_feed_id(
            feed.podcast_index_id,
            max_results=config.ingest.podcastindex.max_episodes_per_request,
            since=since,
        )
        pacer = Pacer(q, config.backlog)
        added = pacer.enqueue_episodes_for_feed(feed.id, episodes)
    typer.echo(f"Backfilled {show}: {added} new episodes enqueued (of {len(episodes)} fetched).")


# ---------------------------------------------------------------------------
# ingest subcommands
# ---------------------------------------------------------------------------


@ingest_app.command("now")
def ingest_now() -> None:
    """One-shot ingest: process all pending episodes."""
    typer.echo(_NOT_YET)
    raise typer.Exit(1)


@ingest_app.command("daemon")
def ingest_daemon() -> None:
    """Start the ingest daemon (APScheduler: poll feeds + run pipeline)."""
    typer.echo(_NOT_YET)
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# top-level commands
# ---------------------------------------------------------------------------


@app.command("transcribe")
def transcribe(
    audio_file: Path = typer.Argument(
        ...,
        help="Path to audio file to transcribe",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
) -> None:
    """Transcribe a single audio file (standalone, no pipeline)."""
    import json

    from pkm.config import load_config
    from pkm.transcribe import from_dict, pick_backend, to_dict

    config = load_config()
    backend = pick_backend(config.compute)
    transcript = backend.transcribe(audio_file)

    out_path = audio_file.with_suffix(audio_file.suffix + ".transcript.json")
    out_path.write_text(json.dumps(to_dict(transcript), indent=2))

    typer.echo(
        f"Transcribed {audio_file} → {len(transcript.segments)} segments,"
        f" {transcript.duration:.1f}s, language={transcript.language}"
    )


@app.command("digest")
def digest(
    period: str = typer.Argument("weekly", help="Digest period (currently only 'weekly')"),
) -> None:
    """Force a synthesis run to produce a digest."""
    typer.echo(_NOT_YET)
    raise typer.Exit(1)


@app.command("query")
def query(
    cypher: str = typer.Argument(..., help="Cypher query to run against the Kuzu graph"),
) -> None:
    """Run an ad-hoc Cypher query against the knowledge graph."""
    from pkm.config import load_config
    from pkm.store.graph import Graph

    config = load_config()
    db_path = Path(config.paths.graph_dir)

    with Graph(db_path) as g:
        g.init_schema()
        rows = g.query(cypher)

    if not rows:
        typer.echo("(no results)")
        return

    cols = list(rows[0].keys())
    # Print header
    typer.echo("\t".join(cols))
    typer.echo("\t".join("-" * len(c) for c in cols))
    for row in rows:
        typer.echo("\t".join(str(row[c]) for c in cols))


@app.command("serve")
def serve() -> None:
    """Start the FastAPI status dashboard at localhost:8765."""
    typer.echo(_NOT_YET)
    raise typer.Exit(1)


@app.command("budget")
def budget() -> None:
    """Show month-to-date Claude API spend."""
    typer.echo(_NOT_YET)
    raise typer.Exit(1)


if __name__ == "__main__":
    app()
