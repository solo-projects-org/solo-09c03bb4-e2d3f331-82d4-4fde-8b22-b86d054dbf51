"""Pydantic request/response models."""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, StringConstraints

Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
FormNo = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]

MAX_ISSUE_FORMS = 20


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


# ------------------------------------------------------------- 核验单 / 凭证
class FormIn(BaseModel):
    form_no: FormNo = Field(description="unique verification-form number")
    year: int = Field(ge=1900, le=2100, description="one of the two base-table years")
    tax_codes: list[Code] = Field(
        min_length=1, description="distinct base-table tariff codes covered by this form"
    )
    total_cents: int = Field(
        ge=0, description="total import amount in integer cents; must match the base table"
    )


class FormOut(BaseModel):
    form_no: str
    year: int
    tax_codes: list[str]
    total_cents: int
    status: Literal["active", "revoked"]
    registered_at: str
    revoked_at: Optional[str] = None


class FormListOut(BaseModel):
    forms: list[FormOut]


class IssueIn(BaseModel):
    observed_version: int = Field(
        ge=1, description="mapping version the caller saw while selecting forms"
    )
    group_id: int = Field(ge=1)
    year: int = Field(ge=1900, le=2100)
    form_nos: list[FormNo] = Field(
        min_length=1,
        max_length=MAX_ISSUE_FORMS,
        description="at most 20 registered form numbers to choose from",
    )


class IssueResult(BaseModel):
    """Result shared by the dry-run preview and the actual issuance."""

    observed_version: int
    group_id: int
    year: int
    selected_form_nos: list[str]
    total_cents: int
    ignored_revoked_form_nos: list[str] = Field(
        description="submitted revoked forms that were excluded; harmless when not needed"
    )


class VoucherOut(IssueResult):
    voucher_no: str
    version: int = Field(description="mapping version in effect when the voucher was issued")
    issued_at: str


class VoucherListOut(BaseModel):
    vouchers: list[VoucherOut]
