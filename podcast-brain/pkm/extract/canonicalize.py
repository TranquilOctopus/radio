from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable

from slugify import slugify


@dataclass(slots=True)
class CanonicalEntity:
    canonical_name: str
    canonical_slug: str
    variants: list[str] = field(default_factory=list)
    count: int = 0


def _slug(name: str) -> str:
    return slugify(name, max_length=80, lowercase=True)


def _similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _pick_canonical_name(variants: list[str]) -> str:
    counts = Counter(variants)
    most_common = counts.most_common()
    top_count = most_common[0][1]
    tied = [name for name, c in most_common if c == top_count]
    return max(tied, key=len)


def canonicalize_names(
    names: Iterable[str],
    *,
    similarity_threshold: float = 0.85,
) -> list[CanonicalEntity]:
    """
    Group near-duplicate name strings into canonical entities by slug similarity.

    Two names are grouped if their slugs match exactly OR have a SequenceMatcher
    ratio >= similarity_threshold.  The canonical name within a group is the most
    frequent variant; ties broken by length.
    """
    cleaned = [n.strip() for n in names if n and n.strip()]
    if not cleaned:
        return []

    # buckets: list of (representative_slug, list_of_variant_strings)
    buckets: list[tuple[str, list[str]]] = []
    for name in cleaned:
        slug = _slug(name)
        if not slug:
            continue
        matched = False
        for i, (rep_slug, variants) in enumerate(buckets):
            if _similarity(slug, rep_slug) >= similarity_threshold:
                variants.append(name)
                matched = True
                break
        if not matched:
            buckets.append((slug, [name]))

    result: list[CanonicalEntity] = []
    for rep_slug, variants in buckets:
        canonical_name = _pick_canonical_name(variants)
        result.append(
            CanonicalEntity(
                canonical_name=canonical_name,
                canonical_slug=_slug(canonical_name),
                variants=sorted(set(variants)),
                count=len(variants),
            )
        )
    result.sort(key=lambda e: (-e.count, e.canonical_slug))
    return result


def name_to_slug_map(entities: list[CanonicalEntity]) -> dict[str, str]:
    """Flatten canonical entities into {original_name: canonical_slug} for quick lookup."""
    out: dict[str, str] = {}
    for e in entities:
        for v in e.variants:
            out[v] = e.canonical_slug
    return out
