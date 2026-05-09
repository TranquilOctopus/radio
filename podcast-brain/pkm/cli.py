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


@feed_app.command("add")
def feed_add(
    url_or_name: str = typer.Argument(..., help="RSS URL, show name, or Apple Podcasts URL"),
    style: str | None = typer.Option(None, "--style", "-s", help="Show style: informational | banter | narrative | skip"),
    auto_style: bool = typer.Option(False, "--auto-style", help="Auto-detect show style on first ingest"),
) -> None:
    """Add a podcast feed by RSS URL, show name, or Apple Podcasts URL."""
    typer.echo(_NOT_YET)
    raise typer.Exit(1)


@feed_app.command("style")
def feed_style(
    show: str = typer.Argument(..., help="Show name or feed identifier"),
    style: str = typer.Argument(..., help="Show style: informational | banter | narrative | skip"),
) -> None:
    """Set the processing style for a subscribed show."""
    typer.echo(_NOT_YET)
    raise typer.Exit(1)


@feed_app.command("import")
def feed_import(
    opml_file: Path = typer.Argument(..., help="Path to OPML export file"),
) -> None:
    """Bulk-import subscriptions from an OPML file."""
    typer.echo(_NOT_YET)
    raise typer.Exit(1)


@feed_app.command("backfill")
def feed_backfill(
    show: str = typer.Argument(..., help="Show name or feed identifier"),
    from_year: str | None = typer.Option(None, "--from", metavar="YYYY", help="Backfill episodes from this year onwards"),
) -> None:
    """Pull full episode history for a show from PodcastIndex."""
    typer.echo(_NOT_YET)
    raise typer.Exit(1)


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
    typer.echo(_NOT_YET)
    raise typer.Exit(1)


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
