"""Pydantic request/response models."""
from __future__ import annotations

from typing import Annotated, Optional

from pydantic import BaseModel, Field, StringConstraints

Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class BaseAmountIn(BaseModel):
    year: int = Field(ge=1900, le=2100, examples=[2023])
    code: Code = Field(examples=["01012100"])
    amount_cents: int = Field(ge=0, description="import amount in integer cents (分)")


class BaseIn(BaseModel):
    amounts: list[BaseAmountIn] = Field(
        min_length=1, description="import amounts for exactly two adjacent years"
    )


class BaseOut(BaseModel):
    years: list[int]
    codes_per_year: dict[str, int]


class MappingEntryIn(BaseModel):
    old_code: Code = Field(description="code in the earlier year")
    new_code: Code = Field(description="code in the later year")


class MappingIn(BaseModel):
    version: int = Field(ge=1, description="monotonically increasing mapping version")
    entries: list[MappingEntryIn] = Field(
        min_length=1,
        description="complete old<->new mapping; unchanged codes appear as (code, code)",
    )


class GroupSummary(BaseModel):
    group_id: int
    old_codes: list[str]
    new_codes: list[str]
    old_total_cents: int = Field(description="total of the earlier year's amounts")
    new_total_cents: int = Field(description="total of the later year's amounts")


class ChangeRecord(BaseModel):
    version: int
    replaced_version: Optional[int] = Field(
        description="version this one replaced; null for the first version"
    )
    removed_group_ids: list[int] = Field(description="group ids to take down")
    summaries: list[GroupSummary] = Field(description="group summaries to write")


class SummaryOut(BaseModel):
    version: Optional[int]
    years: Optional[list[int]]
    groups: list[GroupSummary]


class MappingOut(BaseModel):
    version: int
    replaced_version: Optional[int]
    created_at: str
    entries: list[MappingEntryIn]


class VersionInfo(BaseModel):
    version: int
    replaced_version: Optional[int]
    created_at: str


class VersionListOut(BaseModel):
    current_version: Optional[int]
    versions: list[VersionInfo]
