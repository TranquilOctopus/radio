from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Callable

from slugify import slugify

from pkm.budget import BudgetTracker
from pkm.config import Config
from pkm.extract.base import Extractor
from pkm.extract.canonicalize import CanonicalEntity, canonicalize_names
from pkm.extract.chunker import Chunk, chunk_transcript
from pkm.queue import JobRow, Queue
from pkm.store.graph import (
    ClaimRecord,
    ConceptRecord,
    EpisodeRecord,
    Graph,
    OrganizationRecord,
    PersonRecord,
)
from pkm.store.vault import (
    ClaimPage,
    EpisodePage,
    Vault,
)
from pkm.summarize.base import EpisodeContext, EpisodeSummary
from pkm.summarize.episode import EpisodeSummarizer
from pkm.transcribe.base import Transcript, WhisperBackend, from_dict, to_dict

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentinel that feed add --auto-style stores; we map it to "informational"
# until pkm/extract/classify.py is wired in.
# ---------------------------------------------------------------------------
_AUTO_STYLE_SENTINEL = "__pending_classification"

# ---------------------------------------------------------------------------
# Per-model price table (USD per million tokens).
# Order: input, output, cache_read, cache_write.
# Cache write = 1.25× input; cache read = 0.1× input per prompt-caching docs.
# ---------------------------------------------------------------------------
_PRICES_PER_MTOK: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-4-6":  (3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5":   (1.00,  5.00, 0.10, 1.25),
    "claude-opus-4-7":    (5.00, 25.00, 0.50, 6.25),
}


class _UsageProxy:
    """Adapts EpisodeSummary token counts to the attribute names BudgetTracker.record() expects."""

    def __init__(self, summary: EpisodeSummary) -> None:
        self.input_tokens = summary.input_tokens
        self.output_tokens = summary.output_tokens
        self.cache_read_input_tokens = summary.cache_read_tokens
        self.cache_creation_input_tokens = summary.cache_creation_tokens


def _compute_cost(model: str, summary: EpisodeSummary) -> float:
    prices = _PRICES_PER_MTOK.get(model)
    if prices is None:
        log.warning("No price table entry for model %r; cost recorded as $0.00", model)
        return 0.0
    inp_p, out_p, cr_p, cw_p = prices
    cost = (
        summary.input_tokens       * inp_p / 1_000_000
        + summary.output_tokens    * out_p / 1_000_000
        + summary.cache_read_tokens * cr_p / 1_000_000
        + summary.cache_creation_tokens * cw_p / 1_000_000
    )
    return cost


def _effective_style(raw_style: str) -> str:
    """Map the auto-style sentinel to 'informational' until classify.py is wired."""
    # TODO: plug pkm/extract/classify.py here when auto-detection is implemented.
    if raw_style == _AUTO_STYLE_SENTINEL:
        return "informational"
    return raw_style


def _slug(s: str) -> str:
    return slugify(s, lowercase=True, max_length=80)


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _default_downloader(url: str, dest: Path) -> Path:
    """Thin HTTP download; yt-dlp integration is deferred to Step 11."""
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=120.0) as r:
        r.raise_for_status()
        tmp = dest.parent / (dest.name + ".tmp")
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    os.replace(tmp, dest)
    return dest


def _should_serialize_models(config: Config) -> bool:
    """
    Decide whether Whisper must be unloaded before the LLM loads.

    On a 12 GB card Whisper large-v3 (~3 GB) + a 14B Q4 LLM (~9 GB) = ~12 GB,
    which is right at the limit.  We serialize by default on VRAM < 16 GB so
    the two models are never resident simultaneously.  On a unified-memory Mac
    or a 24 GB card both can live in memory without eviction pressure.
    """
    gate = config.compute.serialize_models
    if gate == "true":
        return True
    if gate == "false":
        return False
    # "auto": inspect CUDA VRAM if available.
    try:
        import torch

        if not torch.cuda.is_available():
            # CPU or MPS path — MLX has no serialization constraint.
            return False
        _, total = torch.cuda.mem_get_info()
        return total < 16 * 1024 ** 3   # < 16 GiB → serialize
    except Exception:
        return False


class Pipeline:
    def __init__(
        self,
        config: Config,
        queue: Queue,
        graph: Graph,
        vault: Vault,
        whisper: WhisperBackend | None = None,
        extractor: Extractor | None = None,
        summarizer: EpisodeSummarizer | None = None,
        budget: BudgetTracker | None = None,
        downloader: Callable[[str, Path], Path] | None = None,
        on_stage_advance: Callable[[int, str, str], None] | None = None,
    ) -> None:
        self._config = config
        self._queue = queue
        self._graph = graph
        self._vault = vault
        self._whisper = whisper
        self._extractor = extractor
        self._summarizer = summarizer
        self._budget = budget
        self._downloader = downloader or _default_downloader
        self._on_stage_advance = on_stage_advance

    # ------------------------------------------------------------------
    # Lazy collaborator accessors
    # ------------------------------------------------------------------

    def _get_whisper(self) -> WhisperBackend:
        if self._whisper is None:
            from pkm.transcribe.base import pick_backend

            self._whisper = pick_backend(self._config.compute)
        return self._whisper

    def _get_extractor(self) -> Extractor:
        if self._extractor is None:
            from pkm.extract.base import get_extractor

            self._extractor = get_extractor(self._config.extract)
        return self._extractor

    def _get_summarizer(self) -> EpisodeSummarizer:
        if self._summarizer is None:
            self._summarizer = EpisodeSummarizer(config=self._config.budget)
        return self._summarizer

    def _get_budget(self) -> BudgetTracker:
        if self._budget is None:
            self._budget = BudgetTracker(
                Path(self._config.paths.db_path), self._config.budget
            )
        return self._budget

    # ------------------------------------------------------------------
    # Artifact paths
    # ------------------------------------------------------------------

    def _audio_path(self, job: JobRow) -> Path:
        ext = Path(job.episode_url.split("?")[0]).suffix or ".mp3"
        name = f"{job.feed_id}_{_slug(job.episode_title)}{ext}"
        return Path(self._config.paths.audio_dir) / name

    def _transcript_path(self, job: JobRow) -> Path:
        return Path(self._config.paths.transcripts_dir) / f"{job.id}.transcript.json"

    def _chunks_path(self, job: JobRow) -> Path:
        return Path(self._config.paths.transcripts_dir) / f"{job.id}.chunks.json"

    def _extractions_path(self, job: JobRow) -> Path:
        return Path(self._config.paths.transcripts_dir) / f"{job.id}.extractions.json"

    def _canonical_path(self, job: JobRow) -> Path:
        return Path(self._config.paths.transcripts_dir) / f"{job.id}.canonical.json"

    def _summary_path(self, job: JobRow) -> Path:
        return Path(self._config.paths.transcripts_dir) / f"{job.id}.summary.md"

    # ------------------------------------------------------------------
    # Status helper
    # ------------------------------------------------------------------

    def _advance(self, job: JobRow, new_status: str) -> None:
        old = job.status
        self._queue.update_job_status(job.id, new_status)
        job.status = new_status
        if self._on_stage_advance:
            self._on_stage_advance(job.id, old, new_status)
        log.debug("job %d: %s → %s", job.id, old, new_status)

    def _fail(self, job: JobRow, exc: Exception) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        log.error("job %d FAILED at %s: %s", job.id, job.status, msg)
        self._queue.update_job_status(job.id, "FAILED", error=msg)
        job.status = "FAILED"

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------

    def _download(self, job: JobRow) -> None:
        dest = self._audio_path(job)
        if not dest.exists():
            self._downloader(job.episode_url, dest)
        self._advance(job, "DOWNLOADED")

    def _transcribe(self, job: JobRow) -> None:
        path = self._transcript_path(job)
        if not path.exists():
            audio = self._audio_path(job)
            whisper = self._get_whisper()
            lang = self._config.transcribe.language or None
            transcript = whisper.transcribe(audio, language=lang)
            _atomic_write_json(path, to_dict(transcript))

        # Serialization: drop the Whisper model from VRAM before we load the LLM.
        # Required on 12 GB cards where Whisper large-v3 (~3 GB) + 14B Q4 (~9 GB)
        # would exceed the card's budget if both were resident simultaneously.
        if _should_serialize_models(self._config) and self._whisper is not None:
            if hasattr(self._whisper, "release"):
                self._whisper.release()
            self._whisper = None

        self._advance(job, "TRANSCRIBED")

    def _chunk(self, job: JobRow) -> None:
        path = self._chunks_path(job)
        if not path.exists():
            transcript_data = json.loads(self._transcript_path(job).read_text())
            transcript: Transcript = from_dict(transcript_data)
            chunks = chunk_transcript(transcript, self._config.chunker)
            chunk_dicts = [
                {"index": c.index, "text": c.text, "start": c.start, "end": c.end, "n_words": c.n_words}
                for c in chunks
            ]
            _atomic_write_json(path, chunk_dicts)
        self._advance(job, "CHUNKED")

    def _extract(self, job: JobRow) -> None:
        path = self._extractions_path(job)
        feed = self._queue.get_feed_by_id(job.feed_id)
        style = _effective_style(feed.style if feed else "informational")

        if style == "skip":
            # Nothing to extract; write an empty list so _canonicalize can proceed.
            if not path.exists():
                _atomic_write_json(path, [])
            self._advance(job, "EXTRACTED")
            return

        if not path.exists():
            chunk_data = json.loads(self._chunks_path(job).read_text())
            chunks = [
                Chunk(
                    index=d["index"],
                    text=d["text"],
                    start=d["start"],
                    end=d["end"],
                    n_words=d["n_words"],
                )
                for d in chunk_data
            ]
            extractor = self._get_extractor()
            language = feed.language if feed else None
            extractions = []
            for chunk in chunks:
                result = extractor.extract_chunk(chunk, style=style, language=language)
                extractions.append(result.model_dump())
            _atomic_write_json(path, extractions)

        self._advance(job, "EXTRACTED")

    def _canonicalize(self, job: JobRow) -> None:
        path = self._canonical_path(job)
        if not path.exists():
            extractions = json.loads(self._extractions_path(job).read_text())

            people_names: list[str] = []
            concept_names: list[str] = []
            org_names: list[str] = []

            for ex in extractions:
                for p in ex.get("people", []):
                    name = p.get("name") if isinstance(p, dict) else str(p)
                    if name:
                        people_names.append(name)
                for c in ex.get("concepts", []):
                    name = c.get("name") if isinstance(c, dict) else str(c)
                    if name:
                        concept_names.append(name)
                for o in ex.get("organizations", []):
                    name = o.get("name") if isinstance(o, dict) else str(o)
                    if name:
                        org_names.append(name)
                # banter-style has mentions as a flat list of strings
                for m in ex.get("mentions", []):
                    if isinstance(m, str) and m:
                        people_names.append(m)

            canonical = {
                "people": [
                    {"canonical_name": e.canonical_name, "canonical_slug": e.canonical_slug,
                     "variants": e.variants, "count": e.count}
                    for e in canonicalize_names(people_names)
                ],
                "concepts": [
                    {"canonical_name": e.canonical_name, "canonical_slug": e.canonical_slug,
                     "variants": e.variants, "count": e.count}
                    for e in canonicalize_names(concept_names)
                ],
                "organizations": [
                    {"canonical_name": e.canonical_name, "canonical_slug": e.canonical_slug,
                     "variants": e.variants, "count": e.count}
                    for e in canonicalize_names(org_names)
                ],
            }
            _atomic_write_json(path, canonical)

        self._advance(job, "CANONICALIZED")

    def _summarize(self, job: JobRow) -> None:
        path = self._summary_path(job)
        feed = self._queue.get_feed_by_id(job.feed_id)
        style = _effective_style(feed.style if feed else "informational")

        if style == "skip":
            # skip-style episodes get a minimal summary placeholder
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.parent / (path.name + ".tmp")
                tmp.write_text("*(listening record only — style=skip)*\n", encoding="utf-8")
                os.replace(tmp, path)
            self._advance(job, "SUMMARIZED")
            return

        if not self._get_budget().can_spend():
            # BUDGET_PAUSED is a terminal-for-now status; daemon does NOT retry.
            # User must clear (Step 9 will add budget reset tooling).
            log.warning("job %d: budget cap reached — pausing", job.id)
            self._advance(job, "BUDGET_PAUSED")
            return

        if not path.exists():
            extractions = json.loads(self._extractions_path(job).read_text())
            ctx = EpisodeContext(
                podcast=feed.title if feed else f"feed-{job.feed_id}",
                title=job.episode_title,
                published=job.episode_published,
                duration_s=job.episode_duration_s,
                style=style,
                language=feed.language if feed else None,
                chunk_extractions=extractions,
            )
            summary = self._get_summarizer().summarize(ctx)

            tmp = path.parent / (path.name + ".tmp")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(summary.markdown, encoding="utf-8")
            os.replace(tmp, path)

            # Record spend immediately so the budget tracker sees it even if
            # subsequent stages fail.
            cost = _compute_cost(summary.model_used, summary)
            # BudgetTracker.record expects an object with the raw anthropic Usage
            # attribute names.  Wrap EpisodeSummary in a thin namespace so we
            # don't import anthropic.types here.
            usage_proxy = _UsageProxy(summary)
            self._get_budget().record(summary.model_used, usage_proxy, cost_usd=cost)  # type: ignore[arg-type]

        self._advance(job, "SUMMARIZED")

    def _index(self, job: JobRow) -> None:
        """Graph upsert + vault write + backlink regen. Combines SUMMARIZED→INDEXED→DONE."""
        feed = self._queue.get_feed_by_id(job.feed_id)
        style = _effective_style(feed.style if feed else "informational")

        canonical_data = json.loads(self._canonical_path(job).read_text())
        extractions = json.loads(self._extractions_path(job).read_text())
        summary_md = self._summary_path(job).read_text(encoding="utf-8")

        podcast_title = feed.title if feed else f"feed-{job.feed_id}"
        podcast_slug = feed.podcast_slug if feed else _slug(podcast_title)
        date_str = (job.episode_published or "1970-01-01")[:10]
        title_slug = _slug(job.episode_title)
        episode_id = f"{podcast_slug}/{date_str}-{title_slug}"

        # --- graph ---
        published_date = _parse_date(date_str)
        self._graph.upsert_episode(
            EpisodeRecord(
                id=episode_id,
                title=job.episode_title,
                podcast=podcast_title,
                published=published_date,
                duration_s=float(job.episode_duration_s or 0),
                audio_path=str(self._audio_path(job)),
                transcript_path=str(self._transcript_path(job)),
            )
        )

        people_canonical = canonical_data.get("people", [])
        concept_canonical = canonical_data.get("concepts", [])
        org_canonical = canonical_data.get("organizations", [])

        for ent in people_canonical:
            self._graph.upsert_person(
                PersonRecord(
                    slug=ent["canonical_slug"],
                    name=ent["canonical_name"],
                    aliases=ent.get("variants", []),
                )
            )
            self._graph.link_mentions(episode_id, ent["canonical_slug"], "person", count=ent["count"], t_first_s=0.0)

        for ent in concept_canonical:
            self._graph.upsert_concept(
                ConceptRecord(slug=ent["canonical_slug"], name=ent["canonical_name"])
            )
            self._graph.link_mentions(episode_id, ent["canonical_slug"], "concept", count=ent["count"], t_first_s=0.0)

        for ent in org_canonical:
            self._graph.upsert_organization(
                OrganizationRecord(slug=ent["canonical_slug"], name=ent["canonical_name"])
            )
            self._graph.link_mentions(episode_id, ent["canonical_slug"], "organization", count=ent["count"], t_first_s=0.0)

        # Claims: only for informational/narrative
        claim_ids: list[tuple[str, str]] = []
        if style in ("informational", "narrative"):
            import hashlib

            for ex in extractions:
                for claim in ex.get("claims", []):
                    text = claim.get("text", "") if isinstance(claim, dict) else str(claim)
                    if not text:
                        continue
                    cid = "c-" + hashlib.sha1(
                        f"{episode_id}:{text}".encode()
                    ).hexdigest()[:12]
                    self._graph.upsert_claim(
                        ClaimRecord(
                            id=cid,
                            text=text,
                            polarity=claim.get("polarity", "") if isinstance(claim, dict) else "",
                            episode_id=episode_id,
                            t_start_s=0.0,
                            source_quote=claim.get("source_quote") or "" if isinstance(claim, dict) else "",
                        )
                    )
                    self._graph.link_contains(episode_id, cid)
                    # ABOUT links for any mentioned concepts
                    for concept_ent in concept_canonical:
                        self._graph.link_about(cid, concept_ent["canonical_slug"], "concept")
                    claim_ids.append((cid, text[:120]))

        # --- vault ---
        ep_page = EpisodePage(
            id=episode_id,
            podcast=podcast_title,
            podcast_slug=podcast_slug,
            title=job.episode_title,
            title_slug=title_slug,
            date=date_str,
            duration_s=float(job.episode_duration_s or 0),
            style=style,
            language=feed.language or "en" if feed else "en",
            concepts=[e["canonical_name"] for e in concept_canonical],
            claims=claim_ids,
            mentioned_people=[e["canonical_name"] for e in people_canonical],
            mentioned_orgs=[e["canonical_name"] for e in org_canonical],
            tldr=_extract_tldr(summary_md),
        )
        self._vault.write_episode(ep_page)

        # Write per-entity pages and backlinks.
        self._vault.regenerate_backlinks(self._graph)

        self._advance(job, "INDEXED")
        self._advance(job, "DONE")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def advance_one(self) -> bool:
        """
        Pull the highest-priority claimable job and advance it through all
        stages until it terminates (DONE / FAILED / BUDGET_PAUSED) or blocks.
        Returns True if any work happened.
        """
        non_terminal = ["PENDING", "DOWNLOADED", "TRANSCRIBED", "CHUNKED", "EXTRACTED", "CANONICALIZED", "SUMMARIZED"]
        job = self._queue.claim_next_job(non_terminal)
        if job is None:
            return False

        _STAGE_MAP: dict[str, Callable[[JobRow], None]] = {
            "PENDING": self._download,
            "DOWNLOADED": self._transcribe,
            "TRANSCRIBED": self._chunk,
            "CHUNKED": self._extract,
            "EXTRACTED": self._canonicalize,
            "CANONICALIZED": self._summarize,
            "SUMMARIZED": self._index,
        }

        # Drive the job all the way through to a terminal status in one call.
        while job.status in _STAGE_MAP:
            stage_fn = _STAGE_MAP[job.status]
            try:
                stage_fn(job)
            except Exception as exc:
                self._fail(job, exc)
                break

        return True

    def run_until_idle(self, max_iterations: int = 1000) -> int:
        count = 0
        for _ in range(max_iterations):
            if not self.advance_one():
                break
            count += 1
        return count


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_date(date_str: str):
    import datetime

    try:
        return datetime.date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return datetime.date(1970, 1, 1)


def _extract_tldr(markdown: str) -> str:
    """Pull the first non-empty paragraph after a TL;DR heading, if present."""
    lines = markdown.splitlines()
    in_tldr = False
    collected: list[str] = []
    for line in lines:
        if line.lower().strip().startswith("## tl;dr") or line.lower().strip().startswith("## tldr"):
            in_tldr = True
            continue
        if in_tldr:
            if line.startswith("## "):
                break
            if line.strip():
                collected.append(line.strip())
    return " ".join(collected) if collected else ""
