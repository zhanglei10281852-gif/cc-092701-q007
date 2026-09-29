from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BatchCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)


class ScoreEntry(BaseModel):
    student_code: str = Field(min_length=1, max_length=64)
    student_name: str = Field(default="", max_length=120)
    total: float
    items: dict[str, float] = Field(default_factory=dict)


class CandidateSubmit(BaseModel):
    scorer_code: str = Field(min_length=1, max_length=64)
    scorer_name: str = Field(default="", max_length=120)
    scores: list[ScoreEntry] = Field(min_length=1)
    metrics: dict[str, Any] = Field(default_factory=dict)


class ReviewRequest(BaseModel):
    passed: bool
    note: str = Field(default="", max_length=2000)
    expected_version: int | None = Field(default=None, ge=1)


class PublishRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)
    expected_version: int | None = Field(default=None, ge=1)


class RevokeRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)
