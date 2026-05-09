from __future__ import annotations

import gc
from pathlib import Path

from pkm.transcribe.base import Segment, Transcript, Word


class FasterWhisperCUDA:
    def __init__(self, model_size: str = "large-v3") -> None:
        self._model_size = model_size
        self._model = None

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self._model_size, device="cuda", compute_type="float16")
        return self._model

    def transcribe(self, audio_path: Path, *, language: str | None = None) -> Transcript:
        model = self._get_model()
        segments_gen, info = model.transcribe(
            str(audio_path),
            language=language,
            word_timestamps=True,
            vad_filter=True,
        )
        segments = [
            Segment(
                text=seg.text,
                start=seg.start,
                end=seg.end,
                words=[
                    Word(
                        text=w.word,
                        start=w.start,
                        end=w.end,
                        probability=w.probability,
                    )
                    for w in (seg.words or [])
                ],
            )
            for seg in segments_gen
        ]
        return Transcript(
            language=info.language,
            duration=info.duration,
            segments=segments,
            model=self._model_size,
            backend="faster-whisper-cuda",
        )

    def release(self) -> None:
        self._model = None
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass


class FasterWhisperCPU:
    def __init__(self, model_size: str = "large-v3") -> None:
        self._model_size = model_size
        self._model = None

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self._model_size, device="cpu", compute_type="int8")
        return self._model

    def transcribe(self, audio_path: Path, *, language: str | None = None) -> Transcript:
        model = self._get_model()
        segments_gen, info = model.transcribe(
            str(audio_path),
            language=language,
            word_timestamps=True,
            vad_filter=True,
        )
        segments = [
            Segment(
                text=seg.text,
                start=seg.start,
                end=seg.end,
                words=[
                    Word(
                        text=w.word,
                        start=w.start,
                        end=w.end,
                        probability=w.probability,
                    )
                    for w in (seg.words or [])
                ],
            )
            for seg in segments_gen
        ]
        return Transcript(
            language=info.language,
            duration=info.duration,
            segments=segments,
            model=self._model_size,
            backend="faster-whisper-cpu",
        )

    def release(self) -> None:
        self._model = None
        gc.collect()
