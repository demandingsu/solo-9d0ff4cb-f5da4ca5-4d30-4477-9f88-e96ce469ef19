"""Pydantic request/response models."""

from __future__ import annotations

import re
from datetime import date
from typing import List

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
