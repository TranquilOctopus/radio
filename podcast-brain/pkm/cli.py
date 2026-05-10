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
def ingest_now(
    config_path: Path | None = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    """One-shot ingest: process all pending episodes."""
    from pkm.config import load_config
    from pkm.pipeline import Pipeline
    from pkm.queue import Queue
    from pkm.store.graph import Graph
    from pkm.store.vault import Vault

    config = load_config(config_path)
    db_path = Path(config.paths.db_path)
    graph_path = Path(config.paths.graph_dir)
    vault_path = Path(config.paths.vault_dir)

    with Queue(db_path) as q:
        q.init_schema()
        with Graph(graph_path) as g:
            g.init_schema()
            vault = Vault(vault_path)

            done: int = 0

            def _on_advance(job_id: int, from_status: str, to_status: str) -> None:
                nonlocal done
                typer.echo(f"  job {job_id}: {from_status} → {to_status}")
                if to_status == "DONE":
                    done += 1

            pipeline = Pipeline(
                config=config,
                queue=q,
                graph=g,
                vault=vault,
                on_stage_advance=_on_advance,
            )
            n = pipeline.run_until_idle()

    typer.echo(f"Processed {done} jobs ({n} iterations).")


@ingest_app.command("daemon")
def ingest_daemon(
    config_path: Path | None = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    """Start the ingest daemon (APScheduler: poll feeds + run pipeline)."""
    import logging

    from apscheduler.schedulers.blocking import BlockingScheduler

    from pkm.config import load_config
    from pkm.ingest.pacer import Pacer
    from pkm.ingest.rss import fetch_feed
    from pkm.pipeline import Pipeline
    from pkm.queue import FeedRow, JobRow, Queue
    from pkm.store.graph import Graph
    from pkm.store.vault import Vault

    logging.basicConfig(level=logging.INFO)
    config = load_config(config_path)

    db_path = Path(config.paths.db_path)
    graph_path = Path(config.paths.graph_dir)
    vault_path = Path(config.paths.vault_dir)

    q = Queue(db_path)
    q.init_schema()
    g = Graph(graph_path)
    g.init_schema()
    vault = Vault(vault_path)

    pipeline = Pipeline(config=config, queue=q, graph=g, vault=vault)

    def _poll_feeds() -> None:
        feeds = q.list_feeds()
        for feed in feeds:
            result = fetch_feed(feed.feed_url)
            if result.status != 200 or not result.episodes:
                continue
            pacer = Pacer(q, config.backlog)
            added = pacer.enqueue_episodes_for_feed(feed.id, result.episodes)
            if added:
                logging.getLogger(__name__).info(
                    "Polled %s: %d new episodes enqueued", feed.title, added
                )
        pacer = Pacer(q, config.backlog)
        pacer.release_next_batch()

    def _run_pipeline() -> None:
        pipeline.advance_one()

    def _weekly_digest() -> None:
        # Step 10 will implement the actual weekly synthesis.
        logging.getLogger(__name__).info("would generate weekly digest (Step 10 pending)")

    scheduler = BlockingScheduler()
    scheduler.add_job(_poll_feeds, "interval", minutes=30, id="poll_feeds")
    scheduler.add_job(_run_pipeline, "interval", minutes=5, id="run_pipeline")
    scheduler.add_job(
        _weekly_digest,
        "cron",
        day_of_week="sun",
        hour=8,
        minute=0,
        id="weekly_digest",
    )

    typer.echo("Daemon starting. Ctrl-C to stop.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        scheduler.shutdown()
        q.close()
        g.close()
        typer.echo("Daemon stopped.")


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
    from pkm.budget import BudgetTracker
    from pkm.config import load_config

    config = load_config()
    with BudgetTracker(Path(config.paths.db_path), config.budget) as t:
        spend = t.mtd_spend_usd()
        cap = config.budget.monthly_cap_usd
        remaining = t.mtd_remaining_usd()
        typer.echo(f"Month-to-date Claude spend: ${spend:.4f}")
        if cap > 0:
            pct = (spend / cap) * 100 if cap else 0
            typer.echo(f"Monthly cap: ${cap:.2f} ({pct:.1f}% used, ${remaining:.4f} remaining)")
        else:
            typer.echo("Monthly cap: unlimited")
        breakdown = t.usage_breakdown()
        if breakdown:
            typer.echo("\nBy model:")
            for row in breakdown:
                typer.echo(
                    f"  {row['model']}: {row['calls']} calls, "
                    f"in={row['input_tokens']}, out={row['output_tokens']}, "
                    f"cache_r={row['cache_read_tokens']}, cache_w={row['cache_write_tokens']}, "
                    f"${row['cost_usd']:.4f}"
                )


if __name__ == "__main__":
    app()
