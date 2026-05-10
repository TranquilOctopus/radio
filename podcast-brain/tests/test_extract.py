from __future__ import annotations

import json
from typing import Iterator

import httpx
import pytest
from pydantic import ValidationError

from pkm.config import ChunkerConfig
from pkm.extract.base import ExtractionError, get_extractor
from pkm.extract.chunker import Chunk, chunk_transcript
from pkm.extract.local import LocalExtractor
from pkm.extract.prompts import load_system_prompt
from pkm.extract.schemas import SCHEMA_BY_STYLE
from pkm.extract.schemas.banter import BanterExtraction
from pkm.extract.schemas.informational import InformationalExtraction
from pkm.extract.schemas.narrative import NarrativeExtraction
from pkm.transcribe.base import Segment, Transcript


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_transcript(segments: list[tuple[float, float, str]]) -> Transcript:
    segs = [Segment(text=t, start=s, end=e) for s, e, t in segments]
    duration = segs[-1].end if segs else 0.0
    return Transcript(language="en", duration=duration, segments=segs, model="test", backend="test")


def _six_minute_transcript() -> Transcript:
    # 12 segments × 30s each = 6 minutes total
    return _make_transcript(
        [(i * 30.0, (i + 1) * 30.0, f"Segment {i} text.") for i in range(12)]
    )


def _make_local_extractor(handler) -> LocalExtractor:
    from pkm.config import ExtractConfig

    config = ExtractConfig(
        backend="local",
        local_model="test-model",
        local_endpoint="http://ollama.test",
        json_mode="json_schema",
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return LocalExtractor(config, client=client)


def _ollama_response(content_dict: dict) -> httpx.Response:
    return httpx.Response(200, json={"message": {"content": json.dumps(content_dict)}})


# ---------------------------------------------------------------------------
# Chunker tests
# ---------------------------------------------------------------------------


def test_chunk_empty_transcript_returns_empty_list() -> None:
    t = Transcript(language="en", duration=0.0, segments=[], model="test", backend="test")
    result = chunk_transcript(t, ChunkerConfig(target_seconds=180, overlap_seconds=15))
    assert result == []


def test_chunk_six_minutes_produces_at_least_two_chunks() -> None:
    t = _six_minute_transcript()
    chunks = chunk_transcript(t, ChunkerConfig(target_seconds=180, overlap_seconds=15))
    assert len(chunks) >= 2


def test_chunk_indices_are_sequential() -> None:
    t = _six_minute_transcript()
    chunks = chunk_transcript(t, ChunkerConfig(target_seconds=180, overlap_seconds=15))
    for i, c in enumerate(chunks):
        assert c.index == i


def test_chunk_timestamps_are_monotonic() -> None:
    t = _six_minute_transcript()
    chunks = chunk_transcript(t, ChunkerConfig(target_seconds=180, overlap_seconds=15))
    for c in chunks:
        assert c.start < c.end
    for prev, nxt in zip(chunks, chunks[1:]):
        # Each chunk starts after the previous chunk started (overlap is expected)
        assert nxt.start > prev.start


def test_chunk_text_covers_all_segments() -> None:
    # Every segment's text must appear in at least one chunk.
    t = _six_minute_transcript()
    chunks = chunk_transcript(t, ChunkerConfig(target_seconds=180, overlap_seconds=15))
    all_chunk_text = " ".join(c.text for c in chunks)
    for seg in t.segments:
        assert seg.text.strip() in all_chunk_text


def test_chunk_overlap_carries_segments_into_next_chunk() -> None:
    # Use 10s segments (shorter than overlap_seconds=15) so the overlap window
    # always spans at least one full segment, guaranteeing carry-over.
    # 30 segments × 10s = 5 minutes total; target=180s → at least 2 chunks.
    t = _make_transcript([(i * 10.0, (i + 1) * 10.0, f"Seg {i}.") for i in range(30)])
    chunks = chunk_transcript(t, ChunkerConfig(target_seconds=180, overlap_seconds=15))
    assert len(chunks) >= 2
    # The second chunk must start strictly before the first chunk ends (overlap).
    assert chunks[1].start < chunks[0].end


def test_chunk_n_words_is_positive_for_nonempty_transcript() -> None:
    t = _six_minute_transcript()
    chunks = chunk_transcript(t, ChunkerConfig(target_seconds=180, overlap_seconds=15))
    for c in chunks:
        assert c.n_words > 0


def test_chunk_single_segment_shorter_than_target() -> None:
    # Single segment that never reaches target_seconds → exactly one chunk.
    t = _make_transcript([(0.0, 60.0, "Only one segment.")])
    chunks = chunk_transcript(t, ChunkerConfig(target_seconds=180, overlap_seconds=15))
    assert len(chunks) == 1
    assert chunks[0].text == "Only one segment."


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_schema_by_style_keys() -> None:
    assert set(SCHEMA_BY_STYLE.keys()) == {"informational", "banter", "narrative"}
    # "skip" must not be present — the extractor is not called for skip episodes
    assert "skip" not in SCHEMA_BY_STYLE


def test_informational_schema_has_json_schema() -> None:
    js = InformationalExtraction.model_json_schema()
    assert isinstance(js, dict)
    assert "properties" in js


def test_banter_schema_has_json_schema() -> None:
    js = BanterExtraction.model_json_schema()
    assert isinstance(js, dict)
    assert "properties" in js


def test_narrative_schema_has_json_schema() -> None:
    js = NarrativeExtraction.model_json_schema()
    assert isinstance(js, dict)
    assert "properties" in js


def test_informational_validates_minimal_payload() -> None:
    obj = InformationalExtraction.model_validate(
        {"people": [], "organizations": [], "concepts": [], "claims": []}
    )
    assert obj.claims == []


def test_banter_validates_minimal_payload() -> None:
    obj = BanterExtraction.model_validate(
        {"quotes": [], "mentions": [], "vibe": None, "recurring_bits": []}
    )
    assert obj.quotes == []


def test_narrative_validates_minimal_payload() -> None:
    obj = NarrativeExtraction.model_validate(
        {"chronology": [], "characters": [], "arc_notes": None}
    )
    assert obj.arc_notes is None


def test_informational_rejects_bogus_claim_polarity() -> None:
    with pytest.raises(ValidationError):
        InformationalExtraction.model_validate(
            {"claims": [{"text": "x", "polarity": "totally_made_up"}]}
        )


# ---------------------------------------------------------------------------
# load_system_prompt tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("style", ["informational", "banter", "narrative"])
def test_load_system_prompt_returns_nonempty_string(style: str) -> None:
    prompt = load_system_prompt(style)
    assert isinstance(prompt, str)
    assert len(prompt) > 50


def test_load_system_prompt_raises_for_skip() -> None:
    with pytest.raises(FileNotFoundError):
        load_system_prompt("skip")


def test_load_system_prompt_raises_for_nonsense() -> None:
    with pytest.raises(FileNotFoundError):
        load_system_prompt("nonsense_style_xyz")


# ---------------------------------------------------------------------------
# LocalExtractor mock tests
# ---------------------------------------------------------------------------


def _sample_chunk() -> Chunk:
    return Chunk(index=0, text="Alice said AI will change everything by 2027.", start=0.0, end=45.0, n_words=9)


def test_local_extractor_name() -> None:
    from pkm.config import ExtractConfig

    config = ExtractConfig(local_model="qwen2.5:14b-instruct-q4_K_M")
    extractor = LocalExtractor(config, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    assert extractor.name() == "local:qwen2.5:14b-instruct-q4_K_M"


def test_local_extractor_valid_response_returns_correct_model() -> None:
    valid_payload = {
        "people": [{"name": "Alice", "role": "guest"}],
        "organizations": [],
        "concepts": [],
        "claims": [
            {
                "text": "AI will change everything",
                "polarity": "assertion",
                "speaker": "Alice",
                "is_prediction": True,
                "timeframe": "by 2027",
                "domain": "tech",
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return _ollama_response(valid_payload)

    extractor = _make_local_extractor(handler)
    result = extractor.extract_chunk(_sample_chunk(), style="informational")
    assert isinstance(result, InformationalExtraction)
    assert result.people[0].name == "Alice"
    assert result.claims[0].is_prediction is True
    assert result.claims[0].timeframe == "by 2027"


def test_local_extractor_request_body_contains_format_with_properties() -> None:
    captured: dict = {}
    valid_payload = {"people": [], "organizations": [], "concepts": [], "claims": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _ollama_response(valid_payload)

    extractor = _make_local_extractor(handler)
    extractor.extract_chunk(_sample_chunk(), style="informational")

    assert "format" in captured["body"]
    assert "properties" in captured["body"]["format"]
    assert captured["body"]["stream"] is False
    assert captured["body"]["options"]["temperature"] == 0.2


def test_local_extractor_language_hint_injected_into_system_prompt() -> None:
    captured: dict = {}
    valid_payload = {"quotes": [], "mentions": [], "vibe": None, "recurring_bits": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _ollama_response(valid_payload)

    extractor = _make_local_extractor(handler)
    extractor.extract_chunk(_sample_chunk(), style="banter", language="sv")

    system_msg = captured["body"]["messages"][0]["content"]
    assert "sv" in system_msg


def test_local_extractor_malformed_json_twice_raises_extraction_error() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        # Return invalid JSON both times
        return httpx.Response(200, json={"message": {"content": "not valid json {{"}})

    extractor = _make_local_extractor(handler)
    with pytest.raises(ExtractionError):
        extractor.extract_chunk(_sample_chunk(), style="informational")

    assert call_count == 2  # tried once, retried once


def test_local_extractor_malformed_then_valid_succeeds() -> None:
    call_count = 0
    valid_payload = {"people": [], "organizations": [], "concepts": [], "claims": []}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: malformed JSON
            return httpx.Response(200, json={"message": {"content": "{ bad json"}})
        return _ollama_response(valid_payload)

    extractor = _make_local_extractor(handler)
    result = extractor.extract_chunk(_sample_chunk(), style="informational")
    assert isinstance(result, InformationalExtraction)
    assert call_count == 2


def test_local_extractor_grammar_mode_raises_not_implemented() -> None:
    from pkm.config import ExtractConfig

    config = ExtractConfig(json_mode="grammar")
    extractor = LocalExtractor(config, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    with pytest.raises(NotImplementedError):
        extractor.extract_chunk(_sample_chunk(), style="informational")


def test_get_extractor_local_returns_local_extractor() -> None:
    from pkm.config import ExtractConfig

    config = ExtractConfig(backend="local")
    extractor = get_extractor(config)
    assert isinstance(extractor, LocalExtractor)


def test_get_extractor_claude_raises_not_implemented() -> None:
    from pkm.config import ExtractConfig

    config = ExtractConfig(backend="claude")
    with pytest.raises(NotImplementedError):
        get_extractor(config)
