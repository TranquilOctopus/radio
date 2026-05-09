from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel
from slugify import slugify as _slugify_lib

if TYPE_CHECKING:
    from pkm.store.graph import Graph


# ---------------------------------------------------------------------------
# Slug helper
# ---------------------------------------------------------------------------


def _slug(s: str) -> str:
    return _slugify_lib(s, lowercase=True, max_length=80)


# ---------------------------------------------------------------------------
# Page models
# ---------------------------------------------------------------------------


class EpisodePage(BaseModel):
    id: str
    podcast: str
    podcast_slug: str
    title: str
    title_slug: str
    date: str          # ISO YYYY-MM-DD
    duration_s: float
    style: str = "informational"
    language: str = "en"
    hosts: list[str] = []
    guests: list[str] = []
    concepts: list[str] = []
    claims: list[tuple[str, str]] = []  # (claim_id, claim_text)
    mentioned_people: list[str] = []
    mentioned_orgs: list[str] = []
    tldr: str = ""


class ConceptPage(BaseModel):
    slug: str
    name: str
    description: str = ""
    mentioned_in: list[str] = []   # episode page paths relative to vault root (no ext)


class PersonPage(BaseModel):
    slug: str
    name: str
    aliases: list[str] = []
    mentioned_in: list[str] = []


class OrganizationPage(BaseModel):
    slug: str
    name: str
    mentioned_in: list[str] = []


class ClaimPage(BaseModel):
    id: str
    text: str
    polarity: str = ""
    episode_id: str = ""
    t_start_s: float = 0.0
    source_quote: str = ""
    about: list[str] = []          # slugs of concepts/people/orgs
    asserted_by: list[str] = []    # person slugs


class DigestPage(BaseModel):
    week: str          # YYYY-Www
    body: str          # pre-rendered markdown body; caller constructs it


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _frontmatter(data: dict) -> str:
    return "---\n" + yaml.safe_dump(data, default_flow_style=False, allow_unicode=True) + "---\n"


def _wikilink(path: str) -> str:
    return f"[[{path}]]"


def _render_episode(page: EpisodePage) -> str:
    fm = _frontmatter({
        "type": "episode",
        "id": page.id,
        "podcast": page.podcast,
        "guest": [f"[[{g}]]" for g in page.guests],
        "host": [f"[[{h}]]" for h in page.hosts],
        "date": page.date,
        "duration_s": page.duration_s,
        "style": page.style,
        "language": page.language,
    })

    sections: list[str] = [fm, f"# {page.title}\n"]

    if page.tldr:
        sections.append(f"## TL;DR\n{page.tldr}\n")

    if page.concepts:
        items = "\n".join(f"- [[concepts/{_slug(c)}]]" for c in page.concepts)
        sections.append(f"## Concepts\n{items}\n")

    if page.claims:
        items = "\n".join(
            f"- [[claims/{cid}]] — {text}" for cid, text in page.claims
        )
        sections.append(f"## Claims\n{items}\n")

    mentioned: list[str] = []
    for h in page.hosts:
        mentioned.append(f"- [[people/{_slug(h)}]] (host)")
    for g in page.guests:
        mentioned.append(f"- [[people/{_slug(g)}]] (guest)")
    for p in page.mentioned_people:
        if p not in page.hosts and p not in page.guests:
            mentioned.append(f"- [[people/{_slug(p)}]]")
    for o in page.mentioned_orgs:
        mentioned.append(f"- [[organizations/{_slug(o)}]]")
    if mentioned:
        sections.append("## Mentioned\n" + "\n".join(mentioned) + "\n")

    return "\n".join(sections)


def _render_concept(page: ConceptPage) -> str:
    fm = _frontmatter({"type": "concept", "slug": page.slug, "name": page.name})
    body = f"# {page.name}\n"
    if page.description:
        body += f"\n{page.description}\n"
    if page.mentioned_in:
        items = "\n".join(f"- [[{ep}]]" for ep in page.mentioned_in)
        body += f"\n## Mentioned in\n{items}\n"
    return fm + "\n" + body


def _render_person(page: PersonPage) -> str:
    fm = _frontmatter({
        "type": "person",
        "slug": page.slug,
        "name": page.name,
        "aliases": page.aliases,
    })
    body = f"# {page.name}\n"
    if page.mentioned_in:
        items = "\n".join(f"- [[{ep}]]" for ep in page.mentioned_in)
        body += f"\n## Mentioned in\n{items}\n"
    return fm + "\n" + body


def _render_organization(page: OrganizationPage) -> str:
    fm = _frontmatter({"type": "organization", "slug": page.slug, "name": page.name})
    body = f"# {page.name}\n"
    if page.mentioned_in:
        items = "\n".join(f"- [[{ep}]]" for ep in page.mentioned_in)
        body += f"\n## Mentioned in\n{items}\n"
    return fm + "\n" + body


def _render_claim(page: ClaimPage) -> str:
    fm = _frontmatter({
        "type": "claim",
        "id": page.id,
        "episode_id": page.episode_id,
        "polarity": page.polarity,
        "t_start_s": page.t_start_s,
    })
    body = f"# Claim: {page.text[:80]}\n\n{page.text}\n"
    if page.source_quote:
        body += f"\n> {page.source_quote}\n"
    if page.asserted_by:
        items = "\n".join(f"- [[people/{s}]]" for s in page.asserted_by)
        body += f"\n## Asserted by\n{items}\n"
    if page.about:
        items = "\n".join(f"- [[{s}]]" for s in page.about)
        body += f"\n## About\n{items}\n"
    return fm + "\n" + body


def _render_digest(page: DigestPage) -> str:
    fm = _frontmatter({"type": "weekly-digest", "week": page.week})
    return fm + "\n" + f"# Weekly Digest — {page.week}\n\n" + page.body


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------


class Vault:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def episode_path(self, podcast_slug: str, date: str, title_slug: str) -> Path:
        return self.root / "episodes" / podcast_slug / f"{date}-{title_slug}.md"

    def page_link(
        self,
        type: Literal["episode", "concept", "person", "organization", "claim"],
        slug: str,
    ) -> str:
        folder_map = {
            "episode": "episodes",
            "concept": "concepts",
            "person": "people",
            "organization": "organizations",
            "claim": "claims",
        }
        return _wikilink(f"{folder_map[type]}/{slug}")

    # ------------------------------------------------------------------
    # Writers
    # ------------------------------------------------------------------

    def write_episode(self, ep: EpisodePage) -> Path:
        path = self.episode_path(ep.podcast_slug, ep.date, ep.title_slug)
        _atomic_write(path, _render_episode(ep))
        return path

    def write_concept(self, c: ConceptPage) -> Path:
        path = self.root / "concepts" / f"{c.slug}.md"
        _atomic_write(path, _render_concept(c))
        return path

    def write_person(self, p: PersonPage) -> Path:
        path = self.root / "people" / f"{p.slug}.md"
        _atomic_write(path, _render_person(p))
        return path

    def write_organization(self, o: OrganizationPage) -> Path:
        path = self.root / "organizations" / f"{o.slug}.md"
        _atomic_write(path, _render_organization(o))
        return path

    def write_claim(self, c: ClaimPage) -> Path:
        path = self.root / "claims" / f"{c.id}.md"
        _atomic_write(path, _render_claim(c))
        return path

    def write_weekly_digest(self, d: DigestPage) -> Path:
        path = self.root / "digests" / "weekly" / f"{d.week}.md"
        _atomic_write(path, _render_digest(d))
        return path

    # ------------------------------------------------------------------
    # Backlink regeneration
    # ------------------------------------------------------------------

    def regenerate_backlinks(self, graph: "Graph") -> None:
        """Walk MENTIONS edges in the graph and rewrite entity 'Mentioned in' sections."""
        # Concepts
        concept_rows = graph.query(
            "MATCH (e:Episode)-[:MENTIONS]->(c:Concept) "
            "RETURN c.slug, c.name, c.description, e.podcast, e.id, e.title, "
            "CAST(e.published AS STRING) AS published"
        )
        self._rewrite_entity_backlinks(concept_rows, "concept")

        # People (via MENTIONS, not HOSTED_BY/GUEST)
        person_rows = graph.query(
            "MATCH (e:Episode)-[:MENTIONS]->(p:Person) "
            "RETURN p.slug, p.name, p.aliases, e.podcast, e.id, e.title, "
            "CAST(e.published AS STRING) AS published"
        )
        self._rewrite_entity_backlinks(person_rows, "person")

        # Organizations
        org_rows = graph.query(
            "MATCH (e:Episode)-[:MENTIONS]->(o:Organization) "
            "RETURN o.slug, o.name, e.podcast, e.id, e.title, "
            "CAST(e.published AS STRING) AS published"
        )
        self._rewrite_entity_backlinks(org_rows, "organization")

    def _rewrite_entity_backlinks(
        self, rows: list[dict], entity_type: Literal["concept", "person", "organization"]
    ) -> None:
        # Group by entity slug
        from collections import defaultdict
        by_slug: dict[str, dict] = {}
        mentions: dict[str, list[str]] = defaultdict(list)

        for row in rows:
            slug = row.get("c.slug") or row.get("p.slug") or row.get("o.slug")
            if slug is None:
                continue
            ep_date = row.get("published", "")[:10]  # first 10 chars = YYYY-MM-DD
            ep_podcast_slug = _slug(row.get("e.podcast", ""))
            ep_title_slug = _slug(row.get("e.title", ""))
            ep_path = f"episodes/{ep_podcast_slug}/{ep_date}-{ep_title_slug}"
            mentions[slug].append(ep_path)
            if slug not in by_slug:
                by_slug[slug] = row

        for slug, row in by_slug.items():
            if entity_type == "concept":
                page = ConceptPage(
                    slug=slug,
                    name=row.get("c.name", slug),
                    description=row.get("c.description", ""),
                    mentioned_in=sorted(set(mentions[slug])),
                )
                self.write_concept(page)
            elif entity_type == "person":
                page = PersonPage(
                    slug=slug,
                    name=row.get("p.name", slug),
                    aliases=row.get("p.aliases", []) or [],
                    mentioned_in=sorted(set(mentions[slug])),
                )
                self.write_person(page)
            else:
                page = OrganizationPage(
                    slug=slug,
                    name=row.get("o.name", slug),
                    mentioned_in=sorted(set(mentions[slug])),
                )
                self.write_organization(page)
