from __future__ import annotations

from pydantic import BaseModel


class EpisodeContext(BaseModel):
    """Everything the summarizer needs: episode metadata + per-chunk extractions."""

    podcast: str
    title: str
    published: str | None  # ISO date
    duration_s: int | None
    style: str  # informational | banter | narrative
    language: str | None  # "en", "sv", etc.; None means unknown
    # Per-chunk extracted JSON (one dict per chunk, in time order).
    # Each chunk dict matches the relevant style schema from pkm/extract/schemas/.
    chunk_extractions: list[dict]
    # Optional: full transcript text (only included when chunk_extractions are sparse)
    transcript_text: str | None = None


class EpisodeSummary(BaseModel):
    """The summarizer's output. Step 8 will turn this into a vault EpisodePage."""

    markdown: str  # the rendered episode wiki page body (NOT including frontmatter)
    model_used: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
