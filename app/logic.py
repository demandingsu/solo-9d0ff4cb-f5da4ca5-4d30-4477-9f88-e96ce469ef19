"""Pure date/interval logic: moving-wall access and access-attempt verification."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, List, Optional, Tuple

_ONE_DAY = timedelta(days=1)


def subtract_months(d: date, months: int) -> date:
    """``d`` shifted back by ``months`` calendar months.

    The day is clamped to the target month's length, so
    2026-03-31 minus 1 month is 2026-02-28.
    """
    if months < 0:
        raise ValueError("months must be >= 0")
    total = d.year * 12 + (d.month - 1) - months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@dataclass(frozen=True)
class CoverageRow:
    package: str
    start: date
    end: date
    embargo_months: int


@dataclass(frozen=True)
class Segment:
    start: date
    end: date
    sources: Tuple[str, ...]


def accessible_interval(row: CoverageRow, query_date: date) -> Tuple[date, date] | None:
    """Accessible part of one coverage row on ``query_date``.

    The embargo is a moving wall: issues published within the most
    recent ``embargo_months`` before the query date are blocked, so the
    accessible window ends at ``query_date - embargo_months`` (and never
    past the row's own coverage end).
    """
    cutoff = subtract_months(query_date, row.embargo_months)
    acc_end = min(row.end, cutoff)
    if row.start > acc_end:
        return None
    return row.start, acc_end


def compute_segments(rows: Iterable[CoverageRow], query_date: date) -> List[Segment]:
    """Merge accessible intervals into segments split at source changes.

    Adjacent days served by the *same set* of packages are merged;
    wherever the contributing package set changes, a new segment starts,
    so every segment lists exactly the packages backing it.
    """
    intervals: List[Tuple[date, date, str]] = []
    for row in rows:
        acc = accessible_interval(row, query_date)
        if acc is not None:
            intervals.append((acc[0], acc[1], row.package))
    if not intervals:
        return []

    boundaries = set()
    for start, end, _ in intervals:
        boundaries.add(start)
        boundaries.add(end + _ONE_DAY)
    ordered = sorted(boundaries)

    segments: List[Segment] = []
    for a, b in zip(ordered, ordered[1:]):
        seg_end = b - _ONE_DAY
        sources = tuple(sorted({pkg for s, e, pkg in intervals if s <= a and e >= seg_end}))
        if not sources:
            continue
        if segments and segments[-1].sources == sources and segments[-1].end + _ONE_DAY == a:
            segments[-1] = Segment(segments[-1].start, seg_end, sources)
        else:
            segments.append(Segment(a, seg_end, sources))
    return segments


# ----------------------------------------------------- access verification
#
# Verification is deliberately recomputed on every query rather than
# stored: when coverage or a license is corrected retroactively, or a
# late attempt is backfilled, the next query re-judges the stored
# attempt against the currently committed state automatically.


@dataclass(frozen=True)
class PackageState:
    """Everything the verifier needs to know about one package."""

    package_id: int
    name: str
    license_start: date
    license_end: date
    active: bool
    coverage: Tuple["CoverageBasis", ...]


@dataclass(frozen=True)
class CoverageBasis:
    """One coverage row of a package, with its moving-wall verdict."""

    start: date
    end: date
    embargo_months: int
    embargo_cutoff: date          # access_date minus embargo_months
    covers_article: bool          # article pub date inside [start, end]
    embargo_blocks: bool          # article pub date later than the cutoff


@dataclass(frozen=True)
class PackageVerdict:
    """Whether one package *should* serve the article on access_date."""

    package_id: int
    name: str
    expected_access: bool
    reason: Optional[str]         # machine code, None when access expected
    basis: Tuple[CoverageBasis, ...]
    license_start: date
    license_end: date
    active: bool
    matched_row: Optional[CoverageBasis]


# Machine-readable reason codes; Chinese explanations live in the API layer.
REASON_PACKAGE_INACTIVE = "package_inactive"
REASON_LICENSE_NOT_STARTED = "license_not_started"
REASON_LICENSE_EXPIRED = "license_expired"
REASON_NO_COVERAGE = "no_coverage_for_issn"
REASON_PUB_BEFORE_COVERAGE = "article_before_coverage_start"
REASON_PUB_AFTER_COVERAGE = "article_after_coverage_end"
REASON_EMBARGO = "embargo_blocks_article"


def evaluate_package(
    state: PackageState,
    article_pub_date: date,
    access_date: date,
) -> PackageVerdict:
    """Compute the expected entitlement of one package for one attempt.

    Check order (and therefore the reported reason) is fixed so the same
    committed data always yields the same verdict.
    """
    if not state.active:
        return PackageVerdict(state.package_id, state.name, False,
                              REASON_PACKAGE_INACTIVE, state.coverage,
                              state.license_start, state.license_end,
                              state.active, None)
    if access_date < state.license_start:
        return PackageVerdict(state.package_id, state.name, False,
                              REASON_LICENSE_NOT_STARTED, state.coverage,
                              state.license_start, state.license_end,
                              state.active, None)
    if access_date > state.license_end:
        return PackageVerdict(state.package_id, state.name, False,
                              REASON_LICENSE_EXPIRED, state.coverage,
                              state.license_start, state.license_end,
                              state.active, None)
    if not state.coverage:
        return PackageVerdict(state.package_id, state.name, False,
                              REASON_NO_COVERAGE, state.coverage,
                              state.license_start, state.license_end,
                              state.active, None)

    covering = [b for b in state.coverage if b.covers_article]
    if not covering:
        # The ISSN is in the package but the article's publication date
        # falls outside every listed range.  Before the earliest row is
        # "before coverage"; anything else (after the last row, or inside
        # a gap between non-overlapping rows) means some coverage window
        # has already ended, so report "after coverage end".
        if article_pub_date < min(b.start for b in state.coverage):
            reason = REASON_PUB_BEFORE_COVERAGE
        else:
            reason = REASON_PUB_AFTER_COVERAGE
        return PackageVerdict(state.package_id, state.name, False, reason,
                              state.coverage, state.license_start,
                              state.license_end, state.active, None)

    # Prefer the most permissive row (shortest embargo, then earliest
    # start): if any row serves the article, the package is entitled.
    best = min(covering, key=lambda b: (b.embargo_months, b.start))
    if best.embargo_blocks:
        return PackageVerdict(state.package_id, state.name, False,
                              REASON_EMBARGO, state.coverage,
                              state.license_start, state.license_end,
                              state.active, best)
    return PackageVerdict(state.package_id, state.name, True, None,
                          state.coverage, state.license_start,
                          state.license_end, state.active, best)


def build_coverage_basis(
    rows: Iterable[Tuple[date, date, int]],
    article_pub_date: date,
    access_date: date,
) -> Tuple[CoverageBasis, ...]:
    """Turn raw coverage rows into annotated basis rows."""
    basis: List[CoverageBasis] = []
    for start, end, embargo_months in rows:
        cutoff = subtract_months(access_date, embargo_months)
        basis.append(CoverageBasis(
            start=start,
            end=end,
            embargo_months=embargo_months,
            embargo_cutoff=cutoff,
            covers_article=start <= article_pub_date <= end,
            embargo_blocks=article_pub_date > cutoff,
        ))
    # Stable, deterministic ordering for API output and tests.
    return tuple(sorted(basis, key=lambda b: (b.start, b.end, b.embargo_months)))


# How the observed outcome compares with the expected entitlement.
MATCH_OK = "consistent"
MATCH_UNEXPECTED_SUCCESS = "unexpected_success"
MATCH_WRONGFUL_DENIAL = "wrongful_denial"
MATCH_EXPECTED_DENIAL = "expected_denial"


def classify_match(outcome: str, expected_access: bool) -> str:
    if expected_access and outcome == "success":
        return MATCH_OK
    if expected_access and outcome == "refused":
        return MATCH_WRONGFUL_DENIAL
    if not expected_access and outcome == "success":
        return MATCH_UNEXPECTED_SUCCESS
    return MATCH_EXPECTED_DENIAL


def library_wide_loss(outcome: str, expected_access: bool, alternatives: Tuple[int, ...]) -> bool:
    """Whether a refusal means the library failed to deliver an entitlement.

    A single package's refusal is never, by itself, a library-wide loss:
    if another package can serve the article, the reader still has a
    working path.  Loss is asserted only when the attempt was refused,
    no alternative package can serve it, and the attempted package was
    in fact entitled -- an entitlement exists somewhere and every path
    to it failed.  A refusal where no package holds any entitlement
    (embargo, unlicensed, not covered) is reported as an expected
    denial, never as loss of access.  Successful attempts never count.
    """
    return outcome == "refused" and expected_access and not alternatives
