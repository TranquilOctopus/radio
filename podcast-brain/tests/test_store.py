from __future__ import annotations

import datetime
import os
from pathlib import Path

import pytest
import yaml

kuzu = pytest.importorskip("kuzu")

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
    ConceptPage,
    DigestPage,
    EpisodePage,
    OrganizationPage,
    PersonPage,
    Vault,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def graph(tmp_path: Path) -> Graph:
    db_path = tmp_path / "test.kuzu"
    g = Graph(db_path)
    g.init_schema()
    yield g
    g.close()


@pytest.fixture()
def vault(tmp_path: Path) -> Vault:
    return Vault(tmp_path / "vault")


# ---------------------------------------------------------------------------
# Graph tests
# ---------------------------------------------------------------------------


def test_init_schema_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "idem.kuzu"
    with Graph(db_path) as g:
        g.init_schema()
        g.init_schema()  # must not raise


def test_hosted_by_two_episodes(graph: Graph) -> None:
    graph.upsert_episode(
        EpisodeRecord(
            id="ep1",
            title="Episode One",
            podcast="Test Show",
            published=datetime.date(2024, 1, 15),
            duration_s=3600.0,
        )
    )
    graph.upsert_episode(
        EpisodeRecord(
            id="ep2",
            title="Episode Two",
            podcast="Test Show",
            published=datetime.date(2024, 2, 1),
            duration_s=1800.0,
        )
    )
    graph.upsert_person(PersonRecord(slug="lex", name="Lex Fridman"))
    graph.link_hosted_by("ep1", "lex")
    graph.link_hosted_by("ep2", "lex")

    rows = graph.query(
        "MATCH (e:Episode)-[:HOSTED_BY]->(p:Person) RETURN e.id, p.name"
    )
    assert len(rows) == 2
    ids = {r["e.id"] for r in rows}
    assert ids == {"ep1", "ep2"}
    assert all(r["p.name"] == "Lex Fridman" for r in rows)


def test_upsert_episode_updates_title(graph: Graph) -> None:
    ep = EpisodeRecord(
        id="ep1",
        title="Original Title",
        podcast="Test Show",
        published=datetime.date(2024, 1, 15),
        duration_s=3600.0,
    )
    graph.upsert_episode(ep)
    ep_updated = ep.model_copy(update={"title": "Updated Title"})
    graph.upsert_episode(ep_updated)

    rows = graph.query("MATCH (e:Episode {id: 'ep1'}) RETURN e.title")
    assert len(rows) == 1
    assert rows[0]["e.title"] == "Updated Title"


def test_mentions_deduplicates_and_accumulates(graph: Graph) -> None:
    graph.upsert_episode(
        EpisodeRecord(
            id="ep1",
            title="Ep",
            podcast="S",
            published=datetime.date(2024, 1, 1),
            duration_s=100.0,
        )
    )
    graph.upsert_concept(ConceptRecord(slug="ai", name="AI"))

    graph.link_mentions("ep1", "ai", "concept", count=2, t_first_s=10.0)
    graph.link_mentions("ep1", "ai", "concept", count=3, t_first_s=5.0)

    rows = graph.query(
        "MATCH (e:Episode)-[r:MENTIONS]->(c:Concept) RETURN r.count, r.t_first_s"
    )
    assert len(rows) == 1
    assert rows[0]["r.count"] == 5
    assert rows[0]["r.t_first_s"] == 5.0


def test_hosted_by_is_idempotent(graph: Graph) -> None:
    """Linking the same (Episode, Person) twice should produce a single edge."""
    graph.upsert_episode(
        EpisodeRecord(
            id="ep1",
            title="E",
            podcast="S",
            published=datetime.date(2024, 1, 1),
            duration_s=60.0,
        )
    )
    graph.upsert_person(PersonRecord(slug="host", name="Host"))
    graph.link_hosted_by("ep1", "host")
    graph.link_hosted_by("ep1", "host")

    rows = graph.query(
        "MATCH (e:Episode)-[:HOSTED_BY]->(p:Person) RETURN count(*) AS n"
    )
    assert rows[0]["n"] == 1


def test_context_manager(tmp_path: Path) -> None:
    db_path = tmp_path / "ctx.kuzu"
    with Graph(db_path) as g:
        g.init_schema()
        g.upsert_concept(ConceptRecord(slug="ml", name="ML"))
        rows = g.query("MATCH (c:Concept) RETURN c.slug")
    assert rows[0]["c.slug"] == "ml"


# ---------------------------------------------------------------------------
# Vault tests
# ---------------------------------------------------------------------------


def _sample_episode_page() -> EpisodePage:
    return EpisodePage(
        id="ep_abc",
        podcast="Lex Fridman",
        podcast_slug="lex-fridman",
        title="Yann LeCun on JEPA",
        title_slug="yann-lecun-on-jepa",
        date="2024-01-15",
        duration_s=7320.0,
        style="informational",
        language="en",
        hosts=["Lex Fridman"],
        guests=["Yann LeCun"],
        concepts=["JEPA", "World Models"],
        claims=[("ab12cd", "JEPA outperforms autoregressive models")],
        mentioned_people=["Yann LeCun"],
        mentioned_orgs=["Meta"],
        tldr="Discussion of JEPA and world models.",
    )


def test_write_episode_creates_file(vault: Vault) -> None:
    page = _sample_episode_page()
    path = vault.write_episode(page)

    assert path.exists()
    text = path.read_text(encoding="utf-8")

    # Frontmatter parses correctly
    parts = text.split("---\n", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["type"] == "episode"
    assert fm["id"] == "ep_abc"
    assert fm["date"] == "2024-01-15"

    # Required sections exist
    assert "## TL;DR" in text
    assert "## Concepts" in text
    assert "## Claims" in text
    assert "## Mentioned" in text
    assert "JEPA" in text
    assert "organizations/meta" in text


def test_write_episode_path_is_deterministic(vault: Vault) -> None:
    page = _sample_episode_page()
    path1 = vault.write_episode(page)
    page2 = page.model_copy(update={"tldr": "Different TL;DR"})
    path2 = vault.write_episode(page2)

    assert path1 == path2
    # Only one file on disk
    parent = path1.parent
    md_files = list(parent.glob("*.md"))
    assert len(md_files) == 1

    # Content reflects the second write
    text = path2.read_text(encoding="utf-8")
    assert "Different TL;DR" in text


def test_write_concept_page(vault: Vault) -> None:
    page = ConceptPage(
        slug="jepa",
        name="JEPA",
        description="Joint-Embedding Predictive Architecture.",
        mentioned_in=["episodes/lex-fridman/2024-01-15-yann-lecun-on-jepa"],
    )
    path = vault.write_concept(page)
    assert path.exists()
    text = path.read_text()
    fm = yaml.safe_load(text.split("---\n", 2)[1])
    assert fm["slug"] == "jepa"
    assert "## Mentioned in" in text
    assert "episodes/lex-fridman/2024-01-15-yann-lecun-on-jepa" in text


def test_regenerate_backlinks(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    db_path = tmp_path / "graph.kuzu"

    with Graph(db_path) as g:
        g.init_schema()

        # Two episodes mentioning the same concept
        for ep_id, title, date in [
            ("ep1", "Episode Alpha", "2024-01-01"),
            ("ep2", "Episode Beta", "2024-02-01"),
        ]:
            g.upsert_episode(
                EpisodeRecord(
                    id=ep_id,
                    title=title,
                    podcast="Test Show",
                    published=datetime.date.fromisoformat(date),
                    duration_s=1800.0,
                )
            )

        g.upsert_concept(ConceptRecord(slug="ml", name="Machine Learning"))
        g.link_mentions("ep1", "ml", "concept", count=1, t_first_s=0.0)
        g.link_mentions("ep2", "ml", "concept", count=1, t_first_s=0.0)

        vault.regenerate_backlinks(g)

    concept_file = tmp_path / "vault" / "concepts" / "ml.md"
    assert concept_file.exists()
    text = concept_file.read_text()
    assert "## Mentioned in" in text
    # Both episode paths appear
    assert "episode-alpha" in text
    assert "episode-beta" in text


def test_atomic_write_no_partial_on_error(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    from pkm.store import vault as vault_mod

    page = _sample_episode_page()
    final_path = vault.episode_path(page.podcast_slug, page.date, page.title_slug)

    call_count = 0

    real_replace = os.replace

    def exploding_replace(src: str, dst: str) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("simulated mid-write failure")

    monkeypatch.setattr(vault_mod.os, "replace", exploding_replace)

    with pytest.raises(RuntimeError, match="simulated"):
        vault.write_episode(page)

    # Final path must NOT exist — only the .tmp would have been written
    assert not final_path.exists()


def test_weekly_digest(vault: Vault) -> None:
    page = DigestPage(week="2024-W03", body="## Top concepts\n- [[concepts/ml]]\n")
    path = vault.write_weekly_digest(page)
    assert path.exists()
    text = path.read_text()
    fm = yaml.safe_load(text.split("---\n", 2)[1])
    assert fm["week"] == "2024-W03"
    assert "concepts/ml" in text
