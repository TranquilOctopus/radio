from __future__ import annotations

import gc
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from pkm.config import ComputeConfig


@dataclass(slots=True)
class Word:
    text: str
    start: float
    end: float
    probability: float | None = None


@dataclass(slots=True)
class Segment:
    text: str
    start: float
    end: float
    words: list[Word] = field(default_factory=list)


@dataclass(slots=True)
class Transcript:
    language: str
    duration: float
    segments: list[Segment]
    model: str
    backend: str


@runtime_checkable
class WhisperBackend(Protocol):
    def transcribe(self, audio_path: Path, *, language: str | None = None) -> Transcript: ...


def pick_backend(config: ComputeConfig) -> WhisperBackend:
    name = config.whisper_backend
    model_size = config.whisper_model

    if name == "mlx" or (name == "auto" and sys.platform == "darwin" and platform.machine() == "arm64"):
        try:
            from pkm.transcribe.mlx_whisper import MLXWhisper
        except ImportError:
            raise ImportError(
                "mlx-whisper is not installed. Install it with: pip install podcast-brain[mac]"
            )
        return MLXWhisper(model_size=model_size)

    if name == "faster-whisper-cuda" or (name == "auto" and _cuda_available()):
        try:
            from pkm.transcribe.faster_whisper import FasterWhisperCUDA
        except ImportError:
            raise ImportError(
                "faster-whisper is not installed. Install it with: pip install podcast-brain[cuda]"
            )
        return FasterWhisperCUDA(model_size=model_size)

    # CPU fallback — also the explicit "faster-whisper-cpu" path
    try:
        from pkm.transcribe.faster_whisper import FasterWhisperCPU
    except ImportError:
        raise ImportError(
            "faster-whisper is not installed. Install it with: pip install podcast-brain[cpu]"
        )
    return FasterWhisperCPU(model_size=model_size)


def _cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


def to_dict(transcript: Transcript) -> dict:
    return {
        "language": transcript.language,
        "duration": transcript.duration,
        "model": transcript.model,
        "backend": transcript.backend,
        "segments": [
            {
                "text": seg.text,
                "start": seg.start,
                "end": seg.end,
                "words": [
                    {
                        "text": w.text,
                        "start": w.start,
                        "end": w.end,
                        "probability": w.probability,
                    }
                    for w in seg.words
                ],
            }
            for seg in transcript.segments
        ],
    }


def from_dict(d: dict) -> Transcript:
    segments = [
        Segment(
            text=seg["text"],
            start=seg["start"],
            end=seg["end"],
            words=[
                Word(
                    text=w["text"],
                    start=w["start"],
                    end=w["end"],
                    probability=w.get("probability"),
                )
                for w in seg.get("words", [])
            ],
        )
        for seg in d.get("segments", [])
    ]
    return Transcript(
        language=d["language"],
        duration=d["duration"],
        segments=segments,
        model=d["model"],
        backend=d["backend"],
    )
