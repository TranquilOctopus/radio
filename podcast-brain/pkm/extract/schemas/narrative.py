from __future__ import annotations

from pydantic import BaseModel, Field


class CharacterMention(BaseModel):
    name: str
    role: str | None = None


class NarrativeExtraction(BaseModel):
    chronology: list[str] = Field(default_factory=list)
    characters: list[CharacterMention] = Field(default_factory=list)
    arc_notes: str | None = None
