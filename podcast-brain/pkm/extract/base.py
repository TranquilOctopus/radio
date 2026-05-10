from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from pkm.config import ExtractConfig
from pkm.extract.chunker import Chunk


class ExtractionError(Exception): ...


@runtime_checkable
class Extractor(Protocol):
    def extract_chunk(self, chunk: Chunk, *, style: str, language: str | None = None) -> BaseModel: ...
    def name(self) -> str: ...


def get_extractor(config: ExtractConfig) -> Extractor:
    if config.backend == "local":
        from pkm.extract.local import LocalExtractor
        return LocalExtractor(config)

    if config.backend == "claude":
        # TODO: Step 7 will wire ClaudeExtractor here.
        raise NotImplementedError("ClaudeExtractor is not yet implemented (Step 7)")

    raise ValueError(f"Unknown extraction backend: {config.backend!r}")
