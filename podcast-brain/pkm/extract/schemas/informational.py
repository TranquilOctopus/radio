from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PersonMention(BaseModel):
    name: str
    role: Literal["host", "guest", "mentioned"] | None = None


class OrganizationMention(BaseModel):
    name: str


class ConceptMention(BaseModel):
    name: str
    description: str | None = None


class ClaimItem(BaseModel):
    text: str
    polarity: Literal["assertion", "denial", "question"] = "assertion"
    speaker: str | None = None
    source_quote: str | None = None
    is_prediction: bool = False
    timeframe: str | None = None
    domain: str | None = None


class InformationalExtraction(BaseModel):
    people: list[PersonMention] = Field(default_factory=list)
    organizations: list[OrganizationMention] = Field(default_factory=list)
    concepts: list[ConceptMention] = Field(default_factory=list)
    claims: list[ClaimItem] = Field(default_factory=list)
