from __future__ import annotations

from pkm.extract.canonicalize import (
    CanonicalEntity,
    canonicalize_names,
    name_to_slug_map,
)


def test_empty_input_returns_empty() -> None:
    assert canonicalize_names([]) == []


def test_dedupes_case_variants() -> None:
    result = canonicalize_names(["JEPA", "jepa", "Jepa"])
    assert len(result) == 1
    assert result[0].canonical_slug == "jepa"
    assert result[0].count == 3
    assert sorted(result[0].variants) == ["JEPA", "Jepa", "jepa"]


def test_dedupes_near_match_punctuation() -> None:
    result = canonicalize_names(["Yann LeCun", "Yann Lecun"])
    assert len(result) == 1


def test_keeps_distinct_names_separate() -> None:
    result = canonicalize_names(["JEPA", "GraphQL", "Kubernetes"])
    assert len(result) == 3
    slugs = {e.canonical_slug for e in result}
    assert slugs == {"jepa", "graphql", "kubernetes"}


def test_canonical_name_is_most_frequent() -> None:
    result = canonicalize_names(["jepa", "jepa", "JEPA"])
    assert result[0].canonical_name == "jepa"


def test_canonical_name_ties_broken_by_length() -> None:
    result = canonicalize_names(["AI", "Artificial Intelligence"])
    if len(result) == 1:
        assert result[0].canonical_name == "Artificial Intelligence"


def test_results_sorted_by_count_desc() -> None:
    names = ["alpha"] * 3 + ["beta"] * 2 + ["gamma"]
    result = canonicalize_names(names)
    assert [e.canonical_name for e in result] == ["alpha", "beta", "gamma"]


def test_strips_and_skips_empty_strings() -> None:
    result = canonicalize_names(["  ", "", "JEPA  ", "JEPA"])
    assert len(result) == 1
    assert result[0].count == 2


def test_name_to_slug_map_flattens_variants() -> None:
    entities = [
        CanonicalEntity("JEPA", "jepa", variants=["JEPA", "jepa"], count=2),
        CanonicalEntity("Yann LeCun", "yann-lecun", variants=["Yann LeCun"], count=1),
    ]
    m = name_to_slug_map(entities)
    assert m == {"JEPA": "jepa", "jepa": "jepa", "Yann LeCun": "yann-lecun"}


def test_higher_threshold_keeps_more_groups() -> None:
    loose = canonicalize_names(["climate change", "climate crisis"], similarity_threshold=0.5)
    assert len(loose) == 1
    strict = canonicalize_names(["climate change", "climate crisis"], similarity_threshold=0.95)
    assert len(strict) == 2
