from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest

kuzu = pytest.importorskip("kuzu")

from pkm.config import Config, SummarizeConfig
from pkm.store.graph import (
    ClaimRecord,
    ConceptRecord,
    EpisodeRecord,
    Graph,
    PersonRecord,
    OrganizationRecord,
)
from pkm.summarize.synthesize import (
    ContradictionCandidate,
    EpisodeSummaryRow,
    SynthesisError,
    WeeklyContext,
    WeeklyDigest,
    WeeklySynthesizer,
    _iso_week,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def graph(tmp_path: Path) -> Graph:
    db_path = tmp_path / "test.kuzu"
    g = Graph(db_path)
    g.init_schema()
    yield g
    g.close()


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.paths.graph_dir = str(tmp_path / "graph.kuzu")
    cfg.paths.vault_dir = str(tmp_path / "vault")
    cfg.paths.db_path = str(tmp_path / "jobs.db")
    return cfg


def _make_mock_client(text: str = "## This week in 3 episodes\nGreat week.") -> MagicMock:
    client = MagicMock(spec=anthropic.Anthropic)
    fake_response = MagicMock()
    fake_response.content = [MagicMock(type="text", text=text)]
    fake_response.model = "claude-sonnet-4-6"
    fake_response.usage = MagicMock(
        input_tokens=2000,
        output_tokens=800,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=1000,
    )
    client.messages.create.return_value = fake_response
    return client


_WEEK_START = date(2026, 4, 27)   # a Monday
_WEEK_END = date(2026, 5, 4)      # exclusive upper bound


def _ep(ep_id: str, title: str, pub: date, podcast: str = "Test Pod") -> EpisodeRecord:
    return EpisodeRecord(
        id=ep_id,
        title=title,
        podcast=podcast,
        published=pub,
        duration_s=3600,
    )


# ---------------------------------------------------------------------------
# Helper: populate a graph for build_context tests
# ---------------------------------------------------------------------------


def _populate_three_episodes(g: Graph) -> tuple[str, str, str]:
    """Insert 3 episodes from the test week; 2 mention the same Concept."""
    ep1 = _ep("pod/2026-04-27-ep-one", "EP One", date(2026, 4, 27))
    ep2 = _ep("pod/2026-04-28-ep-two", "EP Two", date(2026, 4, 28))
    ep3 = _ep("pod/2026-04-29-ep-three", "EP Three", date(2026, 4, 29))
    for ep in (ep1, ep2, ep3):
        g.upsert_episode(ep)

    concept = ConceptRecord(slug="artificial-intelligence", name="Artificial Intelligence")
    g.upsert_concept(concept)

    # ep1 and ep2 mention the same concept → recurring
    g.link_mentions(ep1.id, concept.slug, "concept", 1, 0.0)
    g.link_mentions(ep2.id, concept.slug, "concept", 1, 0.0)

    return ep1.id, ep2.id, ep3.id


# ---------------------------------------------------------------------------
# Tests: build_context
# ---------------------------------------------------------------------------


class TestBuildContext:
    def test_three_episodes_in_week(self, config: Config, tmp_path: Path) -> None:
        with Graph(Path(config.paths.graph_dir)) as g:
            g.init_schema()
            _populate_three_episodes(g)

        synth = WeeklySynthesizer(config, client=_make_mock_client())
        ctx = synth.build_context(_WEEK_START, _WEEK_END)

        assert len(ctx.episodes) == 3
        assert ctx.week_start == _WEEK_START
        assert ctx.week_end == _WEEK_END

    def test_episodes_not_in_week_excluded(self, config: Config, tmp_path: Path) -> None:
        with Graph(Path(config.paths.graph_dir)) as g:
            g.init_schema()
            _populate_three_episodes(g)
            # Add an episode outside the week
            outside = _ep("pod/2026-05-10-outside", "Outside", date(2026, 5, 10))
            g.upsert_episode(outside)

        synth = WeeklySynthesizer(config, client=_make_mock_client())
        ctx = synth.build_context(_WEEK_START, _WEEK_END)

        ids = [ep.id for ep in ctx.episodes]
        assert "pod/2026-05-10-outside" not in ids
        assert len(ctx.episodes) == 3

    def test_one_recurring_concept_with_two_episode_ids(self, config: Config, tmp_path: Path) -> None:
        with Graph(Path(config.paths.graph_dir)) as g:
            g.init_schema()
            ep1_id, ep2_id, _ = _populate_three_episodes(g)

        synth = WeeklySynthesizer(config, client=_make_mock_client())
        ctx = synth.build_context(_WEEK_START, _WEEK_END)

        assert len(ctx.recurring_concepts) == 1
        rc = ctx.recurring_concepts[0]
        assert rc.slug == "artificial-intelligence"
        assert set(rc.episode_ids) == {ep1_id, ep2_id}

    def test_concept_mentioned_once_not_recurring(self, config: Config, tmp_path: Path) -> None:
        with Graph(Path(config.paths.graph_dir)) as g:
            g.init_schema()
            ep1_id, _, _ = _populate_three_episodes(g)

            # Add a second concept mentioned only by ep1
            solo = ConceptRecord(slug="solo-concept", name="Solo Concept")
            g.upsert_concept(solo)
            g.link_mentions(ep1_id, solo.slug, "concept", 1, 0.0)

        synth = WeeklySynthesizer(config, client=_make_mock_client())
        ctx = synth.build_context(_WEEK_START, _WEEK_END)

        slugs = [rc.slug for rc in ctx.recurring_concepts]
        assert "solo-concept" not in slugs

    def test_empty_week_returns_empty_context(self, config: Config, tmp_path: Path) -> None:
        with Graph(Path(config.paths.graph_dir)) as g:
            g.init_schema()
            # No episodes at all

        synth = WeeklySynthesizer(config, client=_make_mock_client())
        ctx = synth.build_context(_WEEK_START, _WEEK_END)

        assert ctx.episodes == []
        assert ctx.recurring_concepts == []


# ---------------------------------------------------------------------------
# Tests: banter exclusion
# ---------------------------------------------------------------------------


class TestBanterExclusion:
    def _config_with_banter_db(self, tmp_path: Path, config: Config) -> None:
        """Write a feeds table with a banter-style feed for 'Test Pod'."""
        import sqlite3

        db_path = Path(config.paths.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS feeds "
            "(id INTEGER PRIMARY KEY, feed_url TEXT, podcast_index_id INTEGER, "
            "itunes_id INTEGER, title TEXT, podcast_slug TEXT, style TEXT, "
            "language TEXT, added_at TEXT, last_polled_at TEXT)"
        )
        conn.execute(
            "INSERT INTO feeds (feed_url, title, podcast_slug, style) "
            "VALUES (?, ?, ?, ?)",
            ("http://banter.example/feed", "Banter Pod", "banter-pod", "banter"),
        )
        conn.commit()
        conn.close()

    def test_banter_episode_excluded_when_flag_set(self, config: Config, tmp_path: Path) -> None:
        config.summarize.exclude_banter_from_digest = True
        self._config_with_banter_db(tmp_path, config)

        with Graph(Path(config.paths.graph_dir)) as g:
            g.init_schema()
            # Regular episode from "Test Pod" (no banter entry in feeds)
            info_ep = _ep("test-pod/2026-04-27-regular", "Regular Episode", date(2026, 4, 27), "Test Pod")
            # Banter episode — podcast_slug matches "banter-pod"
            banter_ep = _ep("banter-pod/2026-04-28-banter", "Banter Episode", date(2026, 4, 28), "Banter Pod")
            for ep in (info_ep, banter_ep):
                g.upsert_episode(ep)

        synth = WeeklySynthesizer(config, client=_make_mock_client())
        ctx = synth.build_context(_WEEK_START, _WEEK_END)

        ids = [ep.id for ep in ctx.episodes]
        assert "banter-pod/2026-04-28-banter" not in ids
        assert "test-pod/2026-04-27-regular" in ids

    def test_banter_concept_mentions_dont_count_toward_recurring(self, config: Config, tmp_path: Path) -> None:
        config.summarize.exclude_banter_from_digest = True
        self._config_with_banter_db(tmp_path, config)

        with Graph(Path(config.paths.graph_dir)) as g:
            g.init_schema()
            # One regular episode, one banter episode, both mention the same concept
            info_ep = _ep("test-pod/2026-04-27-solo", "Solo Ep", date(2026, 4, 27), "Test Pod")
            banter_ep = _ep("banter-pod/2026-04-28-banter", "Banter Ep", date(2026, 4, 28), "Banter Pod")
            for ep in (info_ep, banter_ep):
                g.upsert_episode(ep)

            concept = ConceptRecord(slug="shared-idea", name="Shared Idea")
            g.upsert_concept(concept)
            g.link_mentions(info_ep.id, concept.slug, "concept", 1, 0.0)
            g.link_mentions(banter_ep.id, concept.slug, "concept", 1, 0.0)

        synth = WeeklySynthesizer(config, client=_make_mock_client())
        ctx = synth.build_context(_WEEK_START, _WEEK_END)

        # Only one non-banter episode mentions the concept → not "recurring"
        slugs = [rc.slug for rc in ctx.recurring_concepts]
        assert "shared-idea" not in slugs

    def test_banter_episode_included_when_flag_false(self, config: Config, tmp_path: Path) -> None:
        config.summarize.exclude_banter_from_digest = False
        self._config_with_banter_db(tmp_path, config)

        with Graph(Path(config.paths.graph_dir)) as g:
            g.init_schema()
            banter_ep = _ep("banter-pod/2026-04-28-banter", "Banter Ep", date(2026, 4, 28), "Banter Pod")
            g.upsert_episode(banter_ep)

        synth = WeeklySynthesizer(config, client=_make_mock_client())
        ctx = synth.build_context(_WEEK_START, _WEEK_END)

        ids = [ep.id for ep in ctx.episodes]
        assert "banter-pod/2026-04-28-banter" in ids


# ---------------------------------------------------------------------------
# Tests: contradiction candidates
# ---------------------------------------------------------------------------


class TestContradictionCandidates:
    def test_opposite_polarity_claims_surface_as_candidate(self, config: Config, tmp_path: Path) -> None:
        with Graph(Path(config.paths.graph_dir)) as g:
            g.init_schema()
            ep1 = _ep("pod/2026-04-27-ep1", "Ep1", date(2026, 4, 27))
            ep2 = _ep("pod/2026-04-28-ep2", "Ep2", date(2026, 4, 28))
            g.upsert_episode(ep1)
            g.upsert_episode(ep2)

            concept = ConceptRecord(slug="ai-safety", name="AI Safety")
            g.upsert_concept(concept)

            # assertion vs denial on the same concept
            c1 = ClaimRecord(id="c1", text="AI is safe", polarity="assertion", episode_id=ep1.id)
            c2 = ClaimRecord(id="c2", text="AI is not safe", polarity="denial", episode_id=ep2.id)
            g.upsert_claim(c1)
            g.upsert_claim(c2)

            g.link_about(c1.id, concept.slug, "concept")
            g.link_about(c2.id, concept.slug, "concept")

        synth = WeeklySynthesizer(config, client=_make_mock_client())
        ctx = synth.build_context(_WEEK_START, _WEEK_END)

        assert len(ctx.contradiction_candidates) >= 1
        candidate = ctx.contradiction_candidates[0]
        assert candidate.concept_slug == "ai-safety"
        assert {candidate.claim_a, candidate.claim_b} == {"AI is safe", "AI is not safe"}

    def test_same_polarity_claims_not_a_candidate(self, config: Config, tmp_path: Path) -> None:
        with Graph(Path(config.paths.graph_dir)) as g:
            g.init_schema()
            ep1 = _ep("pod/2026-04-27-ep1", "Ep1", date(2026, 4, 27))
            ep2 = _ep("pod/2026-04-28-ep2", "Ep2", date(2026, 4, 28))
            g.upsert_episode(ep1)
            g.upsert_episode(ep2)

            concept = ConceptRecord(slug="consensus", name="Consensus")
            g.upsert_concept(concept)

            c1 = ClaimRecord(id="c3", text="AI is good", polarity="assertion", episode_id=ep1.id)
            c2 = ClaimRecord(id="c4", text="AI is great", polarity="assertion", episode_id=ep2.id)
            g.upsert_claim(c1)
            g.upsert_claim(c2)
            g.link_about(c1.id, concept.slug, "concept")
            g.link_about(c2.id, concept.slug, "concept")

        synth = WeeklySynthesizer(config, client=_make_mock_client())
        ctx = synth.build_context(_WEEK_START, _WEEK_END)

        assert ctx.contradiction_candidates == []


# ---------------------------------------------------------------------------
# Tests: synthesize (happy path, request shape)
# ---------------------------------------------------------------------------


class TestSynthesize:
    def _minimal_ctx(self) -> WeeklyContext:
        return WeeklyContext(
            week_start=_WEEK_START,
            week_end=_WEEK_END,
            episodes=[
                EpisodeSummaryRow(
                    id="pod/2026-04-27-ep",
                    podcast="Test Pod",
                    title="Test Episode",
                    published="2026-04-27",
                    duration_s=3600,
                    style="informational",
                    tldr_excerpt="Great discussion on AI.",
                )
            ],
            recurring_concepts=[],
            new_people=[],
            new_organizations=[],
            contradiction_candidates=[],
        )

    def test_model_is_sonnet(self) -> None:
        client = _make_mock_client()
        synth = WeeklySynthesizer(Config(), client=client)
        synth.synthesize(self._minimal_ctx())

        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-4-6"

    def test_effort_medium(self) -> None:
        client = _make_mock_client()
        synth = WeeklySynthesizer(Config(), client=client)
        synth.synthesize(self._minimal_ctx())

        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["output_config"] == {"effort": "medium"}

    def test_system_has_cache_control(self) -> None:
        client = _make_mock_client()
        synth = WeeklySynthesizer(Config(), client=client)
        synth.synthesize(self._minimal_ctx())

        kwargs = client.messages.create.call_args.kwargs
        system = kwargs["system"]
        assert isinstance(system, list)
        assert len(system) == 1
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_user_message_is_valid_json_context(self) -> None:
        client = _make_mock_client()
        synth = WeeklySynthesizer(Config(), client=client)
        ctx = self._minimal_ctx()
        synth.synthesize(ctx)

        kwargs = client.messages.create.call_args.kwargs
        payload = json.loads(kwargs["messages"][0]["content"])
        assert "episodes" in payload
        assert len(payload["episodes"]) == 1

    def test_returned_digest_has_markdown(self) -> None:
        expected = "## This week in 1 episodes\n- **Test Pod** — Test Episode"
        client = _make_mock_client(text=expected)
        synth = WeeklySynthesizer(Config(), client=client)
        digest = synth.synthesize(self._minimal_ctx())

        assert isinstance(digest, WeeklyDigest)
        assert digest.markdown == expected
        assert digest.episodes_count == 1
        assert digest.model_used == "claude-sonnet-4-6"
        assert digest.cost_usd_estimate >= 0

    def test_api_status_error_raises_synthesis_error(self) -> None:
        client = MagicMock(spec=anthropic.Anthropic)
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        resp = httpx.Response(500, text="server error", request=req)
        client.messages.create.side_effect = anthropic.APIStatusError(
            "error", response=resp, body=None
        )
        synth = WeeklySynthesizer(Config(), client=client)
        with pytest.raises(SynthesisError):
            synth.synthesize(self._minimal_ctx())

    def test_rate_limit_raises_synthesis_error(self) -> None:
        client = MagicMock(spec=anthropic.Anthropic)
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        resp = httpx.Response(429, text="rate limited", request=req)
        client.messages.create.side_effect = anthropic.RateLimitError(
            "rate limited", response=resp, body=None
        )
        synth = WeeklySynthesizer(Config(), client=client)
        with pytest.raises(SynthesisError):
            synth.synthesize(self._minimal_ctx())


# ---------------------------------------------------------------------------
# Tests: run_for_week end-to-end
# ---------------------------------------------------------------------------


class TestRunForWeek:
    def test_digest_file_written_with_model_output(self, config: Config, tmp_path: Path) -> None:
        # Populate graph with 2 episodes in the week
        with Graph(Path(config.paths.graph_dir)) as g:
            g.init_schema()
            ep1 = _ep("pod/2026-04-27-ep1", "EP One", date(2026, 4, 27))
            ep2 = _ep("pod/2026-04-28-ep2", "EP Two", date(2026, 4, 28))
            g.upsert_episode(ep1)
            g.upsert_episode(ep2)

        expected_markdown = "## This week in 2 episodes\n- bullet 1\n- bullet 2"
        client = _make_mock_client(text=expected_markdown)
        synth = WeeklySynthesizer(config, client=client)
        path = synth.run_for_week(_WEEK_START)

        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert expected_markdown in content

    def test_digest_path_matches_iso_week(self, config: Config, tmp_path: Path) -> None:
        with Graph(Path(config.paths.graph_dir)) as g:
            g.init_schema()

        client = _make_mock_client()
        synth = WeeklySynthesizer(config, client=client)
        path = synth.run_for_week(_WEEK_START)

        # 2026-04-27 is week 18 of 2026
        assert "2026-W18" in str(path)

    def test_digest_has_frontmatter(self, config: Config, tmp_path: Path) -> None:
        with Graph(Path(config.paths.graph_dir)) as g:
            g.init_schema()

        client = _make_mock_client()
        synth = WeeklySynthesizer(config, client=client)
        path = synth.run_for_week(_WEEK_START)

        content = path.read_text(encoding="utf-8")
        assert content.startswith("---")
        assert "type:" in content

    def test_run_with_no_episodes_still_writes_file(self, config: Config, tmp_path: Path) -> None:
        with Graph(Path(config.paths.graph_dir)) as g:
            g.init_schema()
            # No episodes — should synthesize an empty-week digest without crashing

        client = _make_mock_client(text="No episodes this week.")
        synth = WeeklySynthesizer(config, client=client)
        path = synth.run_for_week(_WEEK_START)

        assert path.exists()


# ---------------------------------------------------------------------------
# Tests: TL;DR excerpt extraction
# ---------------------------------------------------------------------------


class TestTldrExcerpt:
    def test_tldr_extracted_from_vault_page(self, config: Config, tmp_path: Path) -> None:
        from pkm.summarize.synthesize import _tldr_excerpt

        vault_root = tmp_path / "vault"
        ep_dir = vault_root / "episodes" / "my-podcast"
        ep_dir.mkdir(parents=True)
        ep_file = ep_dir / "2026-04-27-test-ep.md"
        ep_file.write_text(
            "---\ntype: episode\n---\n# Title\n\n## TL;DR\nThis is a great discussion.\n\n## Claims\n- stuff",
            encoding="utf-8",
        )

        result = _tldr_excerpt(vault_root, "my-podcast", "2026-04-27", "test-ep")
        assert result == "This is a great discussion."

    def test_tldr_capped_at_200_chars(self, config: Config, tmp_path: Path) -> None:
        from pkm.summarize.synthesize import _tldr_excerpt

        vault_root = tmp_path / "vault"
        ep_dir = vault_root / "episodes" / "pod"
        ep_dir.mkdir(parents=True)
        ep_file = ep_dir / "2026-04-27-long.md"
        long_text = "X" * 500
        ep_file.write_text(f"# Title\n\n## TL;DR\n{long_text}\n\n## End", encoding="utf-8")

        result = _tldr_excerpt(vault_root, "pod", "2026-04-27", "long")
        assert len(result) <= 200

    def test_missing_tldr_returns_empty(self, config: Config, tmp_path: Path) -> None:
        from pkm.summarize.synthesize import _tldr_excerpt

        vault_root = tmp_path / "vault"
        ep_dir = vault_root / "episodes" / "pod"
        ep_dir.mkdir(parents=True)
        ep_file = ep_dir / "2026-04-27-notldr.md"
        ep_file.write_text("# No TL;DR here\n\n## Claims\n- stuff", encoding="utf-8")

        result = _tldr_excerpt(vault_root, "pod", "2026-04-27", "notldr")
        assert result == ""

    def test_missing_file_returns_empty(self, config: Config, tmp_path: Path) -> None:
        from pkm.summarize.synthesize import _tldr_excerpt

        result = _tldr_excerpt(tmp_path / "nonexistent-vault", "pod", "2026-04-27", "ep")
        assert result == ""


# ---------------------------------------------------------------------------
# Tests: CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_digest_weekly_specific_week(self, config: Config, tmp_path: Path, monkeypatch) -> None:
        from typer.testing import CliRunner

        from pkm.cli import app

        # Populate graph so context build doesn't fail
        with Graph(Path(config.paths.graph_dir)) as g:
            g.init_schema()

        client = _make_mock_client()

        # Monkeypatch WeeklySynthesizer to inject the mock client
        import pkm.cli as cli_module
        import pkm.summarize.synthesize as synth_module

        original_class = synth_module.WeeklySynthesizer

        def fake_synthesizer(cfg, **kwargs):
            return original_class(cfg, client=client)

        monkeypatch.setattr(synth_module, "WeeklySynthesizer", fake_synthesizer)

        # Monkeypatch load_config to return the test config
        monkeypatch.setattr(cli_module, "digest", cli_module.digest)

        import pkm.config as config_module

        monkeypatch.setattr(config_module, "load_config", lambda p=None: config)

        runner = CliRunner()
        result = runner.invoke(app, ["digest", "weekly", "--week", "2026-W18"])

        assert result.exit_code == 0, result.output
        # The path to the digest file should be echoed
        assert "2026-W18" in result.output

    def test_digest_unknown_period_errors(self) -> None:
        from typer.testing import CliRunner

        from pkm.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["digest", "monthly"])
        assert result.exit_code != 0

    def test_digest_bad_week_format_errors(self) -> None:
        from typer.testing import CliRunner

        from pkm.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["digest", "weekly", "--week", "2026-18"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Utility: _iso_week
# ---------------------------------------------------------------------------


def test_iso_week_format() -> None:
    d = date(2026, 4, 27)  # Week 18 of 2026
    assert _iso_week(d) == "2026-W18"


def test_iso_week_single_digit_padded() -> None:
    d = date(2026, 1, 5)  # Week 2 of 2026
    w = _iso_week(d)
    assert w == "2026-W02"
