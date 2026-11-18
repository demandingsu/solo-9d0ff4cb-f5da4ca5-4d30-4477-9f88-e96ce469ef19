"""Pure date/interval logic for the moving-wall access computation."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, List, Tuple

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
