from __future__ import annotations

from pathlib import Path

import pytest

from pkm.transcribe import (
    Segment,
    Transcript,
    WhisperBackend,
    Word,
    from_dict,
    pick_backend,
    to_dict,
)
from pkm.config import ComputeConfig


class MockWhisperBackend:
    def transcribe(self, audio_path: Path, *, language: str | None = None) -> Transcript:
        return _sample_transcript()


def _sample_transcript() -> Transcript:
    return Transcript(
        language="en",
        duration=12.5,
        segments=[
            Segment(
                text="Hello world.",
                start=0.0,
                end=3.2,
                words=[
                    Word(text="Hello", start=0.0, end=1.1, probability=0.99),
                    Word(text="world.", start=1.2, end=3.2, probability=0.97),
                ],
            ),
            Segment(
                text="Goodbye.",
                start=5.0,
                end=7.0,
                words=[
                    Word(text="Goodbye.", start=5.0, end=7.0, probability=None),
                ],
            ),
        ],
        model="large-v3",
        backend="mock",
    )


def test_round_trip_preserves_all_fields():
    original = _sample_transcript()
    reconstructed = from_dict(to_dict(original))

    assert reconstructed.language == original.language
    assert reconstructed.duration == original.duration
    assert reconstructed.model == original.model
    assert reconstructed.backend == original.backend
    assert len(reconstructed.segments) == len(original.segments)

    seg0 = reconstructed.segments[0]
    assert seg0.text == "Hello world."
    assert seg0.start == 0.0
    assert seg0.end == 3.2
    assert len(seg0.words) == 2

    w0 = seg0.words[0]
    assert w0.text == "Hello"
    assert w0.start == 0.0
    assert w0.end == 1.1
    assert w0.probability == 0.99

    # None probability survives the round-trip
    w_none = reconstructed.segments[1].words[0]
    assert w_none.probability is None


def test_round_trip_empty_words():
    t = Transcript(
        language="sv",
        duration=60.0,
        segments=[Segment(text="Hej.", start=0.0, end=2.0, words=[])],
        model="medium",
        backend="faster-whisper-cpu",
    )
    reconstructed = from_dict(to_dict(t))
    assert reconstructed.segments[0].words == []


def test_mock_satisfies_protocol():
    mock = MockWhisperBackend()
    # runtime_checkable Protocol: isinstance check confirms structural compatibility
    assert isinstance(mock, WhisperBackend)
    result = mock.transcribe(Path("fake.mp3"))
    assert isinstance(result, Transcript)
    assert result.language == "en"


def test_pick_backend_cpu_returns_instance_without_lib_load():
    # FasterWhisperCPU lazy-imports faster_whisper inside transcribe(), not __init__,
    # so construction succeeds even without faster-whisper installed.
    from pkm.transcribe.faster_whisper import FasterWhisperCPU
    backend = FasterWhisperCPU(model_size="large-v3")
    assert backend._model_size == "large-v3"
    assert backend._model is None  # not loaded until transcribe() is called


def test_pick_backend_auto_cpu_fallback(monkeypatch):
    # On this Linux x86_64 machine with no GPU, auto should resolve to FasterWhisperCPU.
    import sys
    import pkm.transcribe.base as base_mod

    # Ensure we're not on darwin/arm64 and cuda is not available
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(base_mod, "_cuda_available", lambda: False)

    config = ComputeConfig(whisper_backend="auto", whisper_model="large-v3")
    backend = pick_backend(config)

    from pkm.transcribe.faster_whisper import FasterWhisperCPU
    assert isinstance(backend, FasterWhisperCPU)


def test_pick_backend_explicit_cpu():
    config = ComputeConfig(whisper_backend="faster-whisper-cpu", whisper_model="large-v3")
    backend = pick_backend(config)
    from pkm.transcribe.faster_whisper import FasterWhisperCPU
    assert isinstance(backend, FasterWhisperCPU)


def test_pick_backend_missing_mlx_raises(monkeypatch):
    import sys
    import pkm.transcribe.base as base_mod

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(base_mod.platform, "machine", lambda: "arm64")

    # Simulate mlx_whisper not importable
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pkm.transcribe.mlx_whisper":
            raise ImportError("No module named 'mlx_whisper'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    config = ComputeConfig(whisper_backend="mlx", whisper_model="large-v3")
    with pytest.raises(ImportError, match="mlx-whisper"):
        pick_backend(config)
