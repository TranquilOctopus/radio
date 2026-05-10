from __future__ import annotations

import importlib.resources
import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import anthropic
from pydantic import BaseModel

from pkm.config import Config


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class EpisodeSummaryRow(BaseModel):
    id: str
    podcast: str
    title: str
    published: str  # ISO date
    duration_s: int
    style: str
    tldr_excerpt: str  # first 200 chars of the TL;DR pulled from the vault episode page


class ConceptCount(BaseModel):
    slug: str
    name: str
    episode_ids: list[str]


class ContradictionCandidate(BaseModel):
    concept_slug: str
    claim_a: str
    episode_a: str
    claim_b: str
    episode_b: str


class WeeklyContext(BaseModel):
    week_start: date
    week_end: date
    episodes: list[EpisodeSummaryRow]
    recurring_concepts: list[ConceptCount]  # concepts mentioned in 2+ episodes this week
    new_people: list[str]                    # people whose first appearance was this week
    new_organizations: list[str]
    contradiction_candidates: list[ContradictionCandidate]  # heuristic pre-filter


class WeeklyDigest(BaseModel):
    week_start: date
    week_end: date
    markdown: str
    episodes_count: int
    model_used: str
    cost_usd_estimate: float


# ---------------------------------------------------------------------------
# Synthesis error
# ---------------------------------------------------------------------------


class SynthesisError(Exception):
    """Raised when the Anthropic API returns an unrecoverable error."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_synthesis_prompt() -> str:
    pkg = importlib.resources.files("pkm.summarize.prompts")
    return (pkg / "weekly_digest.md").read_text(encoding="utf-8")


def _tldr_excerpt(vault_root: Path, podcast_slug: str, date_str: str, title_slug: str) -> str:
    """Pull the first 200 chars after a `## TL;DR` heading in an episode page."""
    ep_path = vault_root / "episodes" / podcast_slug / f"{date_str}-{title_slug}.md"
    if not ep_path.exists():
        return ""
    text = ep_path.read_text(encoding="utf-8")
    # Find the TL;DR heading and grab text after it up to next heading or EOF
    m = re.search(r"## TL;DR\s*\n([\s\S]*?)(?=\n##|\Z)", text)
    if not m:
        return ""
    return m.group(1).strip()[:200]


def _feed_styles(db_path: str) -> dict[str, str]:
    """Return {podcast_slug: style} from the feeds table, or {} if db missing."""
    p = Path(db_path)
    if not p.exists():
        return {}
    conn = sqlite3.connect(str(p))
    try:
        cur = conn.execute("SELECT podcast_slug, style FROM feeds")
        return {row[0]: row[1] for row in cur.fetchall()}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def _iso_week(d: date) -> str:
    """Return ISO week string like 2026-W18."""
    return f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"


# ---------------------------------------------------------------------------
# Cost estimate (Sonnet pricing as of mid-2025)
# ---------------------------------------------------------------------------

_INPUT_PER_TOKEN = 3e-6
_OUTPUT_PER_TOKEN = 15e-6
_CACHE_READ_PER_TOKEN = 0.3e-6
_CACHE_WRITE_PER_TOKEN = 3.75e-6


def _estimate_cost(usage) -> float:
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return inp * _INPUT_PER_TOKEN + out * _OUTPUT_PER_TOKEN + cr * _CACHE_READ_PER_TOKEN + cw * _CACHE_WRITE_PER_TOKEN


# ---------------------------------------------------------------------------
# Synthesizer
# ---------------------------------------------------------------------------


class WeeklySynthesizer:
    def __init__(
        self,
        config: Config,
        client: anthropic.Anthropic | None = None,
        max_tokens: int = 6000,
        effort: str = "medium",
    ) -> None:
        self._config = config
        self._client = client or anthropic.Anthropic()
        self._max_tokens = max_tokens
        self._effort = effort

    # ------------------------------------------------------------------
    # build_context: pure data layer — queries graph + vault
    # ------------------------------------------------------------------

    def build_context(self, week_start: date, week_end: date) -> WeeklyContext:
        from pkm.store.graph import Graph
        from slugify import slugify

        graph_path = Path(self._config.paths.graph_dir)
        vault_root = Path(self._config.paths.vault_dir)
        db_path = self._config.paths.db_path

        # Feed styles for banter exclusion (loaded once, small table)
        feed_styles = _feed_styles(db_path)
        exclude_banter = self._config.summarize.exclude_banter_from_digest

        # Kuzu's Cypher parser rejects `< $param` after `AND` (the `<` is ambiguous
        # with tag syntax `<$var>`). Workaround: inline ISO date literals directly.
        # Dates come from Python's date.isoformat() — always YYYY-MM-DD, no injection risk.
        s = week_start.isoformat()
        e = week_end.isoformat()

        with Graph(graph_path) as g:
            g.init_schema()

            # Episodes in week
            raw_eps = g.query(
                f"MATCH (e:Episode) "
                f"WHERE CAST(e.published AS STRING) >= '{s}' "
                f"AND CAST(e.published AS STRING) < '{e}' "
                f"RETURN e.id, e.title, e.podcast, CAST(e.published AS STRING) AS published, "
                f"CAST(e.duration_s AS INT64) AS duration_s "
                f"ORDER BY e.published",
            )

            episodes: list[EpisodeSummaryRow] = []
            banter_episode_ids: set[str] = set()

            for row in raw_eps:
                ep_id: str = row["e.id"]
                podcast: str = row["e.podcast"]
                title: str = row["e.title"]
                published: str = row["published"][:10]
                duration_s: int = int(row["duration_s"] or 0)

                podcast_slug = slugify(podcast, max_length=60)
                title_slug = slugify(title, max_length=80)

                style = feed_styles.get(podcast_slug, "informational")

                if exclude_banter and style == "banter":
                    banter_episode_ids.add(ep_id)
                    continue

                tldr = _tldr_excerpt(vault_root, podcast_slug, published, title_slug)
                episodes.append(
                    EpisodeSummaryRow(
                        id=ep_id,
                        podcast=podcast,
                        title=title,
                        published=published,
                        duration_s=duration_s,
                        style=style,
                        tldr_excerpt=tldr,
                    )
                )

            included_ids = [ep.id for ep in episodes]

            # Recurring concepts (2+ episodes this week, banter filtered post-query)
            recurring_concepts: list[ConceptCount] = []
            if included_ids:
                raw_concepts = g.query(
                    f"MATCH (e:Episode)-[:MENTIONS]->(c:Concept) "
                    f"WHERE CAST(e.published AS STRING) >= '{s}' "
                    f"AND CAST(e.published AS STRING) < '{e}' "
                    f"WITH c, COLLECT(DISTINCT e.id) AS eps "
                    f"WHERE size(eps) >= 2 "
                    f"RETURN c.slug, c.name, eps "
                    f"ORDER BY size(eps) DESC "
                    f"LIMIT 25",
                )
                included_set = set(included_ids)
                for row in raw_concepts:
                    filtered_eps = [eid for eid in row["eps"] if eid in included_set]
                    if len(filtered_eps) >= 2:
                        recurring_concepts.append(
                            ConceptCount(
                                slug=row["c.slug"],
                                name=row["c.name"],
                                episode_ids=filtered_eps,
                            )
                        )

            # New people this week (first appearance across any episode type)
            # Use UNION because Kuzu's multi-label edge syntax is not universally supported.
            new_people: list[str] = []
            try:
                raw_people = g.query(
                    f"MATCH (p:Person)<-[:GUEST]-(e:Episode) "
                    f"WITH p, MIN(CAST(e.published AS STRING)) AS first_seen "
                    f"WHERE first_seen >= '{s}' AND first_seen < '{e}' "
                    f"RETURN p.name "
                    f"UNION "
                    f"MATCH (p:Person)<-[:HOSTED_BY]-(e:Episode) "
                    f"WITH p, MIN(CAST(e.published AS STRING)) AS first_seen "
                    f"WHERE first_seen >= '{s}' AND first_seen < '{e}' "
                    f"RETURN p.name "
                    f"UNION "
                    f"MATCH (p:Person)<-[:MENTIONS]-(e:Episode) "
                    f"WITH p, MIN(CAST(e.published AS STRING)) AS first_seen "
                    f"WHERE first_seen >= '{s}' AND first_seen < '{e}' "
                    f"RETURN p.name "
                    f"LIMIT 20",
                )
                # new_people is "first appearance in corpus this week" — we need to verify
                # no earlier episode exists. The MIN(published) >= $start check handles that.
                new_people = [r["p.name"] for r in raw_people]
            except Exception:
                # Graph may not have person nodes yet; degrade gracefully.
                new_people = []

            # New organizations this week
            new_organizations: list[str] = []
            try:
                raw_orgs = g.query(
                    f"MATCH (o:Organization)<-[:MENTIONS]-(e:Episode) "
                    f"WITH o, MIN(CAST(e.published AS STRING)) AS first_seen "
                    f"WHERE first_seen >= '{s}' AND first_seen < '{e}' "
                    f"RETURN o.name "
                    f"LIMIT 20",
                )
                new_organizations = [r["o.name"] for r in raw_orgs]
            except Exception:
                new_organizations = []

            # Contradiction candidates: opposite-polarity claims about the same concept
            contradiction_candidates: list[ContradictionCandidate] = []
            if included_ids:
                try:
                    raw_contradictions = g.query(
                        "MATCH (c1:Claim)-[:ABOUT]->(concept:Concept)<-[:ABOUT]-(c2:Claim) "
                        "WHERE c1.id < c2.id "
                        "AND c1.polarity <> c2.polarity "
                        "AND c1.polarity <> '' AND c2.polarity <> '' "
                        "AND c1.episode_id IN $week_ids "
                        "AND c2.episode_id IN $week_ids "
                        "RETURN concept.slug, c1.text, c1.episode_id, c2.text, c2.episode_id "
                        "LIMIT 10",
                        {"week_ids": included_ids},
                    )
                    for row in raw_contradictions:
                        contradiction_candidates.append(
                            ContradictionCandidate(
                                concept_slug=row["concept.slug"],
                                claim_a=row["c1.text"],
                                episode_a=row["c1.episode_id"],
                                claim_b=row["c2.text"],
                                episode_b=row["c2.episode_id"],
                            )
                        )
                except Exception:
                    # Claim or ABOUT edges may be absent; degrade gracefully.
                    contradiction_candidates = []

        return WeeklyContext(
            week_start=week_start,
            week_end=week_end,
            episodes=episodes,
            recurring_concepts=recurring_concepts,
            new_people=new_people,
            new_organizations=new_organizations,
            contradiction_candidates=contradiction_candidates,
        )

    # ------------------------------------------------------------------
    # synthesize: one Sonnet call over the WeeklyContext
    # ------------------------------------------------------------------

    def synthesize(self, ctx: WeeklyContext) -> WeeklyDigest:
        system_prompt = _load_synthesis_prompt()

        # Stable system prompt cached at the block level — same prompt every week.
        system = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        user_message = json.dumps(
            ctx.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )

        try:
            response = self._client.messages.create(
                model=self._config.budget.summarize_model,
                max_tokens=self._max_tokens,
                output_config={"effort": self._effort},
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
        except anthropic.RateLimitError as exc:
            raise SynthesisError(f"Rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise SynthesisError(f"API error {exc.status_code}: {exc}") from exc

        cost = _estimate_cost(response.usage)
        return WeeklyDigest(
            week_start=ctx.week_start,
            week_end=ctx.week_end,
            markdown=response.content[0].text,
            episodes_count=len(ctx.episodes),
            model_used=response.model,
            cost_usd_estimate=cost,
        )

    # ------------------------------------------------------------------
    # run_for_week: build context → synthesize → write to vault
    # ------------------------------------------------------------------

    def run_for_week(self, week_start: date) -> Path:
        week_end = week_start + timedelta(days=7)  # exclusive upper bound for queries
        ctx = self.build_context(week_start, week_end)

        digest = self.synthesize(ctx)

        from pkm.store.vault import DigestPage, Vault

        vault = Vault(Path(self._config.paths.vault_dir))
        week_str = _iso_week(week_start)
        page = DigestPage(week=week_str, body=digest.markdown)
        path = vault.write_weekly_digest(page)
        return path
