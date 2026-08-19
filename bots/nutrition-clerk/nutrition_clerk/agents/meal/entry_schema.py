"""Pydantic models for the four entry shapes defined in REFACTOR-BOT.MD §6.

At M3 only shape (1) — text-only lookup — actually flows through. The other
shapes are declared here so the meal agent's instruction can reference a
stable vocabulary, and so later milestones (M4/M6/M7) can enable them by
extending the agent's instruction and toolset without changing this file.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class BaseEntry(BaseModel):
    """Fields shared by every entry shape."""

    food: str = Field(description="Canonical or free-text food name")
    save_to_cache: bool = Field(
        default=False, description="If True, upsert into personal-foods after logging."
    )


class LookupEntry(BaseEntry):
    """Shape 6.1: text-only, resolved via personal/popular cache."""

    kind: str = Field(default="lookup", frozen=True)
    qty: float = Field(default=1.0, description="Quantity in the resolved unit")


class MacrosEntry(BaseEntry):
    """Shape 6.2: user typed full macros."""

    kind: str = Field(default="macros", frozen=True)
    qty: float
    kcal: float
    protein: float
    fat: float
    carbs: float


class PhotoEntry(BaseEntry):
    """Shape 6.3: macros come from a photo of a label. M7."""

    kind: str = Field(default="photo", frozen=True)
    qty: float
    photo_path: str


class RecentSaveEntry(BaseEntry):
    """Shape 6.4: save a recent meal to the personal cache. M6."""

    kind: str = Field(default="recent_save", frozen=True)
    save_to_cache: bool = True


AnyEntry = LookupEntry | MacrosEntry | PhotoEntry | RecentSaveEntry
