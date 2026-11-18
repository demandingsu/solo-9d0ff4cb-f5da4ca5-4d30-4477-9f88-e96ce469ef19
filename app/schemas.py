"""Pydantic request/response models."""

from __future__ import annotations

import re
from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

_ISSN_RE = re.compile(r"^\d{4}-?\d{3}[\dXx]$")


def normalize_issn(value: str) -> str:
    """Accept ``12345678`` / ``1234-5678`` / ``1234-567X``; store ``1234-567X``."""
    if not isinstance(value, str):
        raise ValueError("ISSN must be a string")
    v = value.strip().upper()
    if not _ISSN_RE.match(v):
        raise ValueError("invalid ISSN: expected 7 digits + digit/X, e.g. 1234-5678")
    return f"{v[:4]}-{v[-4:]}"


class CoverageEntry(BaseModel):
    issn: str
    start_date: date
    end_date: date
    embargo_months: int = Field(default=0, ge=0)

    @field_validator("issn")
    @classmethod
    def _issn(cls, v: str) -> str:
        return normalize_issn(v)

    @model_validator(mode="after")
    def _range(self) -> "CoverageEntry":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class CoverageSnapshot(BaseModel):
    """Full replacement snapshot for one package (empty list clears it)."""

    entries: List[CoverageEntry]


class PackageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    license_start: date
    license_end: date

    @model_validator(mode="after")
    def _range(self) -> "PackageCreate":
        if self.license_end < self.license_start:
            raise ValueError("license_end must be on or after license_start")
        return self


class LicenseUpdate(BaseModel):
    license_start: date
    license_end: date

    @model_validator(mode="after")
    def _range(self) -> "LicenseUpdate":
        if self.license_end < self.license_start:
            raise ValueError("license_end must be on or after license_start")
        return self


class PackageOut(BaseModel):
    id: int
    name: str
    license_start: date
    license_end: date
    active: bool
    coverage_rows: int


class SegmentOut(BaseModel):
    start: date
    end: date
    sources: List[str]


class AccessResponse(BaseModel):
    issn: str
    date: date
    data_version: int
    segments: List[SegmentOut]


# ------------------------------------------------------- access attempts

class AccessAttemptCreate(BaseModel):
    """One reader access attempt, keyed by the reporter's request number."""

    request_no: str = Field(min_length=1, max_length=100,
                            description="Reporter-side unique request number; same number is idempotent")
    access_date: date = Field(description="Day the reader attempted access, YYYY-MM-DD")
    issn: str
    article_pub_date: date = Field(description="Publication date of the article, YYYY-MM-DD")
    package_id: int = Field(gt=0, description="Resource package the reader tried")
    outcome: str = Field(description="Observed result: success or refused")

    @field_validator("issn")
    @classmethod
    def _issn(cls, v: str) -> str:
        return normalize_issn(v)

    @field_validator("outcome")
    @classmethod
    def _outcome(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("success", "refused"):
            raise ValueError("outcome must be 'success' or 'refused'")
        return v


class AccessAttemptOut(BaseModel):
    request_no: str
    access_date: date
    issn: str
    article_pub_date: date
    package_id: int
    outcome: str
    created_at: str
    created_version: int
    data_version: int
    duplicate: bool = False


class CoverageBasisOut(BaseModel):
    start: date
    end: date
    embargo_months: int
    embargo_cutoff: date
    covers_article: bool
    embargo_blocks: bool


class AlternativePackageOut(BaseModel):
    package_id: int
    name: str
    matched_coverage: Optional[CoverageBasisOut] = None


class VerificationBasisOut(BaseModel):
    package_id: int
    package_name: str
    active: bool
    license_start: date
    license_end: date
    license_covers_access_date: bool
    coverage_rows: List[CoverageBasisOut]
    matched_coverage: Optional[CoverageBasisOut] = None


class VerificationItem(BaseModel):
    request_no: str
    access_date: date
    issn: str
    article_pub_date: date
    package_id: int
    package_name: str
    observed_outcome: str
    expected_access: bool
    expected_reason: Optional[str] = None
    expected_reason_text: Optional[str] = None
    match_result: str
    mismatch_reason: Optional[str] = None
    mismatch_reason_text: Optional[str] = None
    alternative_packages: List[AlternativePackageOut]
    library_wide_access_loss: bool
    basis: VerificationBasisOut


class VerificationResponse(BaseModel):
    data_version: int
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    count: int
    items: List[VerificationItem]
