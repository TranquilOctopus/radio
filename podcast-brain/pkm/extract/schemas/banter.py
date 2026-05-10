from __future__ import annotations

from pydantic import BaseModel, Field


class QuoteItem(BaseModel):
    text: str
    speaker: str | None = None


class BanterExtraction(BaseModel):
    quotes: list[QuoteItem] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)
    vibe: str | None = None
    recurring_bits: list[str] = Field(default_factory=list)
