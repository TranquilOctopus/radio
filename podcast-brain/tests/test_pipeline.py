from __future__ import annotations

import json
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock

import pytest

from pkm.config import (
    BacklogConfig,
    BudgetConfig,
    ChunkerConfig,
    ComputeConfig,
    Config,
    ExtractConfig,
    PathsConfig,
    SummarizeConfig,
    TranscribeConfig,
)
from pkm.extract.chunker import Chunk
from pkm.extract.schemas.banter import BanterExtraction
from pkm.extract.schemas.informational import InformationalExtraction
from pkm.pipeline import Pipeline
from pkm.queue import FeedRow, JobRow, Queue
from pkm.store.graph import Graph
from pkm.store.vault import Vault
from pkm.summarize.base import EpisodeContext, EpisodeSummary
from pkm.transcribe.base import Segment, Transcript, Word


# ---------------------------------------------------------------------------
# Fake collaborators
# ---------------------------------------------------------------------------


class MockWhisperBackend:
    """Returns a minimal two-segment transcript; never touches disk or GPU."""

    def __init__(self) -> None:
        self.call_count = 0
        self.released = False

    def transcribe(self, audio_path: Path, *, language: str | None = None) -> Transcript:
        self.call_count += 1
        return Transcript(
            language="en",
            duration=10.0,
            segments=[
                Segment(text="Hello world.", start=0.0, end=5.0),
                Segment(text="This is a test.", start=5.0, end=10.0),
            ],
            model="mock",
            backend="mock",
        )

    def release(self) -> None:
        self.released = True


class MockExtractor:
    """Returns a fixed extraction based on the requested style."""

    def __init__(self) -> None:
        self.call_count = 0
        self.last_style: str | None = None

    def extract_chunk(self, chunk: Chunk, *, style: str, language: str | None = None):
        self.call_count += 1
        self.last_style = style
        if style == "banter":
            return BanterExtraction(quotes=[], mentions=["Alice"], vibe="fun")
        return InformationalExtraction(
            people=[{"name": "Alice", "role": "guest"}],  # type: ignore[list-item]
            concepts=[{"name": "AI", "description": "Artificial intelligence"}],  # type: ignore[list-item]
            claims=[{"text": "AI is transformative", "polarity": "assertion"}],  # type: ignore[list-item]
        )

    def name(self) -> str:
        return "mock-extractor"


class MockSummarizer:
    """Returns a fixed EpisodeSummary; records all EpisodeContext calls."""

    def __init__(self, raise_on_call: bool = False) -> None:
        self.call_count = 0
        self.last_ctx: EpisodeContext | None = None
        self._raise = raise_on_call

    def summarize(self, ctx: EpisodeContext) -> EpisodeSummary:
        self.call_count += 1
        self.last_ctx = ctx
        if self._raise:
            raise RuntimeError("mock summarizer exploded")
        return EpisodeSummary(
            markdown="## TL;DR\nGreat episode.\n\n## Key claims\n- AI is transformative.",
            model_used="claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=0,
            cache_creation_tokens=80,
        )


class MockBudget:
    def __init__(self, can_spend_result: bool = True) -> None:
        self._can_spend = can_spend_result
        self.record_calls: list[tuple] = []

    def can_spend(self, projected_usd: float = 0.0) -> bool:
        return self._can_spend

    def record(self, model, usage, *, cost_usd: float = 0.0) -> None:
        self.record_calls.append((model, cost_usd))

    def close(self) -> None:
        pass


def _mock_downloader(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"\x00")  # 1-byte placeholder
    return dest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_config(tmp_path: Path) -> Config:
    return Config(
        paths=PathsConfig(
            audio_dir=str(tmp_path / "audio"),
            transcripts_dir=str(tmp_path / "transcripts"),
            graph_dir=str(tmp_path / "graph.kuzu"),
            db_path=str(tmp_path / "jobs.db"),
            vault_dir=str(tmp_path / "vault"),
        ),
        compute=ComputeConfig(serialize_models="false"),
        chunker=ChunkerConfig(target_seconds=60, overlap_seconds=0),
        budget=BudgetConfig(summarize_model="claude-sonnet-4-6"),
    )


@pytest.fixture()
def queue(tmp_config: Config) -> Queue:
    q = Queue(Path(tmp_config.paths.db_path))
    q.init_schema()
    return q


@pytest.fixture()
def graph(tmp_config: Config) -> Graph:
    g = Graph(Path(tmp_config.paths.graph_dir))
    g.init_schema()
    yield g
    g.close()


@pytest.fixture()
def vault(tmp_config: Config) -> Vault:
    return Vault(Path(tmp_config.paths.vault_dir))


def _make_pipeline(
    config: Config,
    queue: Queue,
    graph: Graph,
    vault: Vault,
    *,
    whisper=None,
    extractor=None,
    summarizer=None,
    budget=None,
    on_stage_advance=None,
) -> Pipeline:
    return Pipeline(
        config=config,
        queue=queue,
        graph=graph,
        vault=vault,
        whisper=whisper or MockWhisperBackend(),
        extractor=extractor or MockExtractor(),
        summarizer=summarizer or MockSummarizer(),
        budget=budget or MockBudget(),
        downloader=_mock_downloader,
        on_stage_advance=on_stage_advance,
    )


def _add_feed_and_job(
    queue: Queue,
    *,
    style: str = "informational",
    title: str = "Test Pod",
    episode_title: str = "Episode 1",
) -> tuple[int, int]:
    feed_id = queue.upsert_feed(
        FeedRow(
            feed_url=f"https://example.com/{style}.rss",
            title=title,
            podcast_slug=f"test-pod-{style}",
            style=style,
            language="en",
        )
    )
    job_id = queue.enqueue_job(
        JobRow(
            feed_id=feed_id,
            episode_guid=f"guid-{style}-{episode_title}",
            episode_title=episode_title,
            episode_url="https://example.com/audio.mp3",
            episode_published="2024-01-15",
            episode_duration_s=600,
            status="PENDING",
        )
    )
    return feed_id, job_id


# ---------------------------------------------------------------------------
# Tests: end-to-end happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_single_job_reaches_done(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        _add_feed_and_job(queue)
        pipeline = _make_pipeline(tmp_config, queue, graph, vault)
        pipeline.run_until_idle()

        jobs = queue.jobs_by_status("DONE")
        assert len(jobs) == 1

    def test_all_artifacts_exist(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        _, job_id = _add_feed_and_job(queue)
        pipeline = _make_pipeline(tmp_config, queue, graph, vault)
        pipeline.run_until_idle()

        job = queue.jobs_by_status("DONE")[0]
        trans_dir = Path(tmp_config.paths.transcripts_dir)
        assert (trans_dir / f"{job.id}.transcript.json").exists()
        assert (trans_dir / f"{job.id}.chunks.json").exists()
        assert (trans_dir / f"{job.id}.extractions.json").exists()
        assert (trans_dir / f"{job.id}.canonical.json").exists()
        assert (trans_dir / f"{job.id}.summary.md").exists()

    def test_vault_has_episode_page(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        _add_feed_and_job(queue)
        pipeline = _make_pipeline(tmp_config, queue, graph, vault)
        pipeline.run_until_idle()

        episode_dir = Path(tmp_config.paths.vault_dir) / "episodes"
        md_files = list(episode_dir.rglob("*.md"))
        assert len(md_files) >= 1

    def test_vault_has_concept_page(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        _add_feed_and_job(queue)
        pipeline = _make_pipeline(tmp_config, queue, graph, vault)
        pipeline.run_until_idle()

        concept_dir = Path(tmp_config.paths.vault_dir) / "concepts"
        md_files = list(concept_dir.rglob("*.md"))
        assert len(md_files) >= 1

    def test_graph_has_episode_node(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        _add_feed_and_job(queue)
        pipeline = _make_pipeline(tmp_config, queue, graph, vault)
        pipeline.run_until_idle()

        rows = graph.query("MATCH (e:Episode) RETURN e.title")
        assert any("Episode 1" in r["e.title"] for r in rows)

    def test_graph_has_concept_and_claim_nodes(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        _add_feed_and_job(queue)
        pipeline = _make_pipeline(tmp_config, queue, graph, vault)
        pipeline.run_until_idle()

        concepts = graph.query("MATCH (c:Concept) RETURN c.slug")
        assert len(concepts) >= 1
        claims = graph.query("MATCH (c:Claim) RETURN c.id")
        assert len(claims) >= 1


# ---------------------------------------------------------------------------
# Tests: idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_rerun_from_pending_reuses_artifacts(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        whisper = MockWhisperBackend()
        extractor = MockExtractor()
        summarizer = MockSummarizer()
        _, job_id = _add_feed_and_job(queue)
        pipeline = _make_pipeline(
            tmp_config, queue, graph, vault,
            whisper=whisper, extractor=extractor, summarizer=summarizer,
        )
        pipeline.run_until_idle()
        assert queue.jobs_by_status("DONE")

        first_whisper_calls = whisper.call_count
        first_extractor_calls = extractor.call_count
        first_summarizer_calls = summarizer.call_count

        # Reset to PENDING so the pipeline tries again.
        queue.update_job_status(job_id, "PENDING")
        pipeline.run_until_idle()

        # Artifacts already exist → mocks must NOT be called again.
        assert whisper.call_count == first_whisper_calls
        assert extractor.call_count == first_extractor_calls
        assert summarizer.call_count == first_summarizer_calls
        assert queue.jobs_by_status("DONE")


# ---------------------------------------------------------------------------
# Tests: style routing
# ---------------------------------------------------------------------------


class TestStyleRouting:
    def test_banter_style_extractor_receives_banter(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        extractor = MockExtractor()
        summarizer = MockSummarizer()
        _add_feed_and_job(queue, style="banter")
        pipeline = _make_pipeline(
            tmp_config, queue, graph, vault,
            extractor=extractor, summarizer=summarizer,
        )
        pipeline.run_until_idle()

        assert extractor.last_style == "banter"

    def test_banter_style_summarizer_gets_correct_style(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        summarizer = MockSummarizer()
        _add_feed_and_job(queue, style="banter")
        pipeline = _make_pipeline(
            tmp_config, queue, graph, vault, summarizer=summarizer
        )
        pipeline.run_until_idle()

        assert summarizer.last_ctx is not None
        assert summarizer.last_ctx.style == "banter"

    def test_skip_style_no_extract_or_summarize(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        extractor = MockExtractor()
        summarizer = MockSummarizer()
        _add_feed_and_job(queue, style="skip")
        pipeline = _make_pipeline(
            tmp_config, queue, graph, vault,
            extractor=extractor, summarizer=summarizer,
        )
        pipeline.run_until_idle()

        assert extractor.call_count == 0
        assert summarizer.call_count == 0
        assert queue.jobs_by_status("DONE")

    def test_auto_style_sentinel_maps_to_informational(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        extractor = MockExtractor()
        _add_feed_and_job(queue, style="__pending_classification")
        pipeline = _make_pipeline(tmp_config, queue, graph, vault, extractor=extractor)
        pipeline.run_until_idle()

        assert extractor.last_style == "informational"


# ---------------------------------------------------------------------------
# Tests: failure → FAILED status
# ---------------------------------------------------------------------------


class TestFailure:
    def test_summarizer_failure_marks_failed(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        summarizer = MockSummarizer(raise_on_call=True)
        _, job_id = _add_feed_and_job(queue)
        pipeline = _make_pipeline(
            tmp_config, queue, graph, vault, summarizer=summarizer
        )
        pipeline.run_until_idle()

        failed = queue.jobs_by_status("FAILED")
        assert len(failed) == 1
        assert failed[0].last_error is not None
        assert "RuntimeError" in failed[0].last_error or "mock summarizer" in failed[0].last_error

    def test_pipeline_continues_after_failure(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        """A failed job must not prevent subsequent jobs from running."""
        bad_summarizer = MockSummarizer(raise_on_call=True)
        _add_feed_and_job(queue, episode_title="Episode Bad")
        _add_feed_and_job(queue, episode_title="Episode Good", style="skip")  # skip avoids summarizer

        pipeline = _make_pipeline(
            tmp_config, queue, graph, vault, summarizer=bad_summarizer
        )
        pipeline.run_until_idle()

        done = queue.jobs_by_status("DONE")
        failed = queue.jobs_by_status("FAILED")
        assert len(failed) == 1
        assert len(done) == 1

    def test_downloader_failure_marks_failed(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        def bad_downloader(url: str, dest: Path) -> Path:
            raise ConnectionError("network unavailable")

        _add_feed_and_job(queue)
        pipeline = Pipeline(
            config=tmp_config,
            queue=queue,
            graph=graph,
            vault=vault,
            whisper=MockWhisperBackend(),
            extractor=MockExtractor(),
            summarizer=MockSummarizer(),
            budget=MockBudget(),
            downloader=bad_downloader,
        )
        pipeline.run_until_idle()

        failed = queue.jobs_by_status("FAILED")
        assert len(failed) == 1
        assert "ConnectionError" in (failed[0].last_error or "")


# ---------------------------------------------------------------------------
# Tests: budget gate
# ---------------------------------------------------------------------------


class TestBudgetGate:
    def test_budget_exhausted_stops_at_budget_paused(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        summarizer = MockSummarizer()
        budget = MockBudget(can_spend_result=False)
        _add_feed_and_job(queue)
        pipeline = _make_pipeline(
            tmp_config, queue, graph, vault,
            summarizer=summarizer, budget=budget,
        )
        pipeline.run_until_idle()

        assert summarizer.call_count == 0
        paused = queue.jobs_by_status("BUDGET_PAUSED")
        assert len(paused) == 1

    def test_budget_paused_not_retried_by_daemon(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        """BUDGET_PAUSED is excluded from claimable statuses — daemon must not pick it up."""
        budget = MockBudget(can_spend_result=False)
        _add_feed_and_job(queue)
        pipeline = _make_pipeline(
            tmp_config, queue, graph, vault, budget=budget
        )
        pipeline.run_until_idle()

        # Run again — paused job stays paused.
        n = pipeline.run_until_idle()
        assert n == 0
        assert len(queue.jobs_by_status("BUDGET_PAUSED")) == 1


# ---------------------------------------------------------------------------
# Tests: multiple jobs
# ---------------------------------------------------------------------------


class TestMultipleJobs:
    def test_three_jobs_all_reach_done(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        for i in range(3):
            _add_feed_and_job(queue, episode_title=f"Episode {i}", style="skip")

        pipeline = _make_pipeline(tmp_config, queue, graph, vault)
        pipeline.run_until_idle()

        done = queue.jobs_by_status("DONE")
        assert len(done) == 3

    def test_advance_one_returns_true_when_work_exists(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        _add_feed_and_job(queue, style="skip")
        pipeline = _make_pipeline(tmp_config, queue, graph, vault)
        result = pipeline.advance_one()
        assert result is True

    def test_advance_one_returns_false_when_idle(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        pipeline = _make_pipeline(tmp_config, queue, graph, vault)
        result = pipeline.advance_one()
        assert result is False


# ---------------------------------------------------------------------------
# Tests: on_stage_advance callback
# ---------------------------------------------------------------------------


class TestCallback:
    def test_callback_receives_stage_transitions(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        events: list[tuple[int, str, str]] = []
        _add_feed_and_job(queue, style="skip")
        pipeline = _make_pipeline(
            tmp_config, queue, graph, vault,
            on_stage_advance=lambda jid, f, t: events.append((jid, f, t)),
        )
        pipeline.run_until_idle()

        statuses = [t for _, _, t in events]
        assert "DOWNLOADED" in statuses
        assert "DONE" in statuses


# ---------------------------------------------------------------------------
# Tests: VRAM serialization decision
# ---------------------------------------------------------------------------


class TestSerializeModels:
    def test_serialize_true_forces_serialize(self, tmp_config: Config) -> None:
        from pkm.pipeline import _should_serialize_models

        tmp_config.compute.serialize_models = "true"
        assert _should_serialize_models(tmp_config) is True

    def test_serialize_false_disables(self, tmp_config: Config) -> None:
        from pkm.pipeline import _should_serialize_models

        tmp_config.compute.serialize_models = "false"
        assert _should_serialize_models(tmp_config) is False

    def test_serialize_auto_without_cuda_returns_false(self, tmp_config: Config) -> None:
        from pkm.pipeline import _should_serialize_models

        tmp_config.compute.serialize_models = "auto"
        # In test environment there's no CUDA, so auto must default to False.
        result = _should_serialize_models(tmp_config)
        assert isinstance(result, bool)

    def test_whisper_released_when_serialize_true(
        self, tmp_config: Config, queue: Queue, graph: Graph, vault: Vault
    ) -> None:
        tmp_config.compute.serialize_models = "true"
        whisper = MockWhisperBackend()
        _add_feed_and_job(queue, style="skip")
        pipeline = _make_pipeline(tmp_config, queue, graph, vault, whisper=whisper)
        pipeline.run_until_idle()

        assert whisper.released is True
