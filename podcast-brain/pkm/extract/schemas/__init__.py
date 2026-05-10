from __future__ import annotations

from pydantic import BaseModel

from pkm.extract.schemas.banter import BanterExtraction
from pkm.extract.schemas.informational import InformationalExtraction
from pkm.extract.schemas.narrative import NarrativeExtraction

SCHEMA_BY_STYLE: dict[str, type[BaseModel]] = {
    "informational": InformationalExtraction,
    "banter": BanterExtraction,
    "narrative": NarrativeExtraction,
}

__all__ = [
    "SCHEMA_BY_STYLE",
    "InformationalExtraction",
    "BanterExtraction",
    "NarrativeExtraction",
]
