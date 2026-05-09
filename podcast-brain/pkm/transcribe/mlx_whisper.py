from __future__ import annotations

from pathlib import Path

from pkm.transcribe.base import Segment, Transcript, Word

# API ref: https://github.com/ml-explore/mlx-examples/tree/main/whisper
# mlx_whisper.transcribe returns a dict with keys: text, segments, language.
# Each segment dict: {id, seek, start, end, text, tokens, temperature,
#   avg_logprob, compression_ratio, no_speech_prob, words: [{word, start, end, probability}]}.


class MLXWhisper:
    def __init__(self, model_size: str = "large-v3") -> None:
        # model_size maps to an HF repo; only "large-v3" is mapped here by default.
        self._model_repo = _MODEL_REPOS.get(model_size, f"mlx-community/whisper-{model_size}-mlx")

    def transcribe(self, audio_path: Path, *, language: str | None = None) -> Transcript:
        try:
            import mlx_whisper
        except ImportError:
            raise ImportError(
                "mlx-whisper is not installed. Install it with: pip install podcast-brain[mac]"
            )
        kwargs: dict = {
            "path_or_hf_repo": self._model_repo,
            "word_timestamps": True,
        }
        if language is not None:
            kwargs["language"] = language

        result = mlx_whisper.transcribe(str(audio_path), **kwargs)

        segments = [
            Segment(
                text=seg["text"],
                start=seg["start"],
                end=seg["end"],
                words=[
                    Word(
                        text=w["word"],
                        start=w["start"],
                        end=w["end"],
                        probability=w.get("probability"),
                    )
                    for w in seg.get("words", [])
                ],
            )
            for seg in result.get("segments", [])
        ]

        # mlx_whisper doesn't expose duration directly; derive from last segment.
        duration = segments[-1].end if segments else 0.0

        return Transcript(
            language=result.get("language", ""),
            duration=duration,
            segments=segments,
            model=self._model_repo,
            backend="mlx-whisper",
        )

    def release(self) -> None:
        # MLX manages memory via its own allocator; no equivalent to cuda.empty_cache().
        pass


_MODEL_REPOS: dict[str, str] = {
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "tiny": "mlx-community/whisper-tiny-mlx",
}
