from __future__ import annotations

import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

import kuzu


# ---------------------------------------------------------------------------
# Record models
# ---------------------------------------------------------------------------


class EpisodeRecord(BaseModel):
    id: str
    title: str
    podcast: str
    published: datetime.date
    duration_s: float
    audio_path: str = ""
    transcript_path: str = ""


class PersonRecord(BaseModel):
    slug: str
    name: str
    aliases: list[str] = []


class OrganizationRecord(BaseModel):
    slug: str
    name: str


class ConceptRecord(BaseModel):
    slug: str
    name: str
    description: str = ""


class ClaimRecord(BaseModel):
    id: str
    text: str
    polarity: str = ""
    episode_id: str = ""
    t_start_s: float = 0.0
    source_quote: str = ""


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_NODE_TABLES = [
    (
        "Episode",
        "id STRING PRIMARY KEY, title STRING, podcast STRING, "
        "published DATE, duration_s DOUBLE, audio_path STRING, transcript_path STRING",
    ),
    (
        "Person",
        "slug STRING PRIMARY KEY, name STRING, aliases STRING[]",
    ),
    (
        "Organization",
        "slug STRING PRIMARY KEY, name STRING",
    ),
    (
        "Concept",
        "slug STRING PRIMARY KEY, name STRING, description STRING",
    ),
    (
        "Claim",
        "id STRING PRIMARY KEY, text STRING, polarity STRING, "
        "episode_id STRING, t_start_s DOUBLE, source_quote STRING",
    ),
]

_REL_TABLES = [
    ("HOSTED_BY", "FROM Episode TO Person"),
    ("GUEST", "FROM Episode TO Person"),
    (
        "MENTIONS",
        "FROM Episode TO Concept, FROM Episode TO Person, FROM Episode TO Organization, "
        "count INT64, t_first_s DOUBLE",
    ),
    ("CONTAINS", "FROM Episode TO Claim"),
    (
        "ABOUT",
        "FROM Claim TO Concept, FROM Claim TO Person, FROM Claim TO Organization",
    ),
    ("ASSERTED_BY", "FROM Claim TO Person"),
    ("CONTRADICTS", "FROM Claim TO Claim"),
]


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


class Graph:
    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        # Kuzu creates its own directory; only ensure the parent exists.
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = kuzu.Database(str(db_path))
        self._conn = kuzu.Connection(self._db)

    def init_schema(self) -> None:
        for name, cols in _NODE_TABLES:
            self._conn.execute(
                f"CREATE NODE TABLE IF NOT EXISTS {name} ({cols})"
            )
        for name, definition in _REL_TABLES:
            self._conn.execute(
                f"CREATE REL TABLE IF NOT EXISTS {name} ({definition})"
            )

    # ------------------------------------------------------------------
    # Generic upsert helpers
    # ------------------------------------------------------------------

    def _upsert_node(self, table: str, pk_col: str, props: dict) -> None:
        """MERGE a node; ON CREATE and ON MATCH apply all non-PK props.

        Kuzu does not allow re-setting the primary-key column via SET, so we
        exclude it from the clause and rely on the MERGE pattern itself to
        write it on creation.
        """
        non_pk = {k: v for k, v in props.items() if k != pk_col}
        if non_pk:
            set_clause = ", ".join(f"n.{k} = ${k}" for k in non_pk)
            self._conn.execute(
                f"MERGE (n:{table} {{{pk_col}: ${pk_col}}}) "
                f"ON CREATE SET {set_clause} "
                f"ON MATCH SET {set_clause}",
                props,
            )
        else:
            # Only-PK node (unlikely, but safe)
            self._conn.execute(
                f"MERGE (n:{table} {{{pk_col}: ${pk_col}}})",
                {pk_col: props[pk_col]},
            )

    def _upsert_simple_rel(
        self,
        src_table: str,
        src_pk: str,
        src_val: str,
        dst_table: str,
        dst_pk: str,
        dst_val: str,
        rel: str,
    ) -> None:
        self._conn.execute(
            f"MATCH (s:{src_table} {{{src_pk}: $sv}}), "
            f"(d:{dst_table} {{{dst_pk}: $dv}}) "
            f"MERGE (s)-[:{rel}]->(d)",
            {"sv": src_val, "dv": dst_val},
        )

    # ------------------------------------------------------------------
    # Node upserts
    # ------------------------------------------------------------------

    def upsert_episode(self, episode: EpisodeRecord) -> None:
        self._upsert_node("Episode", "id", episode.model_dump())

    def upsert_person(self, person: PersonRecord) -> None:
        self._upsert_node("Person", "slug", person.model_dump())

    def upsert_organization(self, org: OrganizationRecord) -> None:
        self._upsert_node("Organization", "slug", org.model_dump())

    def upsert_concept(self, concept: ConceptRecord) -> None:
        self._upsert_node("Concept", "slug", concept.model_dump())

    def upsert_claim(self, claim: ClaimRecord) -> None:
        self._upsert_node("Claim", "id", claim.model_dump())

    # ------------------------------------------------------------------
    # Relationship upserts
    # ------------------------------------------------------------------

    def link_hosted_by(self, episode_id: str, person_slug: str) -> None:
        self._upsert_simple_rel(
            "Episode", "id", episode_id, "Person", "slug", person_slug, "HOSTED_BY"
        )

    def link_guest(self, episode_id: str, person_slug: str) -> None:
        self._upsert_simple_rel(
            "Episode", "id", episode_id, "Person", "slug", person_slug, "GUEST"
        )

    def link_mentions(
        self,
        episode_id: str,
        target_slug: str,
        target_type: Literal["concept", "person", "organization"],
        count: int,
        t_first_s: float,
    ) -> None:
        table_map = {"concept": "Concept", "person": "Person", "organization": "Organization"}
        pk_map = {"concept": "slug", "person": "slug", "organization": "slug"}
        dst_table = table_map[target_type]
        dst_pk = pk_map[target_type]
        self._conn.execute(
            f"MATCH (e:Episode {{id: $eid}}), (d:{dst_table} {{{dst_pk}: $dv}}) "
            "MERGE (e)-[r:MENTIONS]->(d) "
            "ON CREATE SET r.count = $count, r.t_first_s = $t "
            "ON MATCH SET r.count = r.count + $count, "
            "r.t_first_s = CASE WHEN r.t_first_s < $t THEN r.t_first_s ELSE $t END",
            {"eid": episode_id, "dv": target_slug, "count": count, "t": t_first_s},
        )

    def link_contains(self, episode_id: str, claim_id: str) -> None:
        self._upsert_simple_rel(
            "Episode", "id", episode_id, "Claim", "id", claim_id, "CONTAINS"
        )

    def link_about(
        self,
        claim_id: str,
        target_slug: str,
        target_type: Literal["concept", "person", "organization"],
    ) -> None:
        table_map = {"concept": "Concept", "person": "Person", "organization": "Organization"}
        pk_map = {"concept": "slug", "person": "slug", "organization": "slug"}
        dst_table = table_map[target_type]
        dst_pk = pk_map[target_type]
        self._conn.execute(
            f"MATCH (c:Claim {{id: $cid}}), (d:{dst_table} {{{dst_pk}: $dv}}) "
            "MERGE (c)-[:ABOUT]->(d)",
            {"cid": claim_id, "dv": target_slug},
        )

    def link_asserted_by(self, claim_id: str, person_slug: str) -> None:
        self._upsert_simple_rel(
            "Claim", "id", claim_id, "Person", "slug", person_slug, "ASSERTED_BY"
        )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(self, cypher: str, params: dict | None = None) -> list[dict]:
        result = self._conn.execute(cypher, params or {})
        cols = result.get_column_names()
        rows: list[dict] = []
        while result.has_next():
            rows.append(dict(zip(cols, result.get_next())))
        return rows

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()
        self._db.close()

    def __enter__(self) -> "Graph":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
