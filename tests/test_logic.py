"""Unit tests for the pure segment/moving-wall logic."""

from datetime import date

from app.logic import CoverageRow, compute_segments, subtract_months


def test_subtract_months_plain():
    assert subtract_months(date(2026, 11, 18), 6) == date(2026, 5, 18)
    assert subtract_months(date(2026, 1, 31), 1) == date(2025, 12, 31)
    assert subtract_months(date(2026, 11, 18), 0) == date(2026, 11, 18)


def test_subtract_months_clamps_to_short_month():
    assert subtract_months(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert subtract_months(date(2024, 3, 31), 1) == date(2024, 2, 29)  # leap year


def test_moving_wall_follows_query_date():
    rows = [CoverageRow("A", date(2018, 1, 1), date(2026, 12, 31), 6)]
    seg = compute_segments(rows, date(2026, 11, 18))
    assert seg == [seg[0]]  # single segment
    assert (seg[0].start, seg[0].end) == (date(2018, 1, 1), date(2026, 5, 18))
    # three months later the wall has moved three months too
    seg2 = compute_segments(rows, date(2027, 2, 18))
    assert (seg2[0].start, seg2[0].end) == (date(2018, 1, 1), date(2026, 8, 18))


def test_everything_embargoed_yields_nothing():
    rows = [CoverageRow("A", date(2026, 1, 1), date(2026, 12, 31), 12)]
    assert compute_segments(rows, date(2026, 6, 1)) == []


def test_overlap_splits_at_source_change():
    rows = [
        CoverageRow("A", date(2015, 1, 1), date(2024, 12, 31), 0),
        CoverageRow("B", date(2020, 1, 1), date(2024, 12, 31), 0),
    ]
    seg = compute_segments(rows, date(2026, 1, 1))
    assert [(s.start, s.end, s.sources) for s in seg] == [
        (date(2015, 1, 1), date(2019, 12, 31), ("A",)),
        (date(2020, 1, 1), date(2024, 12, 31), ("A", "B")),
    ]


def test_adjacent_rows_same_package_merge():
    rows = [
        CoverageRow("A", date(2010, 1, 1), date(2010, 12, 31), 0),
        CoverageRow("A", date(2011, 1, 1), date(2011, 12, 31), 0),
    ]
    seg = compute_segments(rows, date(2026, 1, 1))
    assert len(seg) == 1
    assert (seg[0].start, seg[0].end) == (date(2010, 1, 1), date(2011, 12, 31))


def test_different_embargoes_split_sources():
    rows = [
        CoverageRow("A", date(2020, 1, 1), date(2026, 12, 31), 0),
        CoverageRow("B", date(2020, 1, 1), date(2026, 12, 31), 12),
    ]
    seg = compute_segments(rows, date(2026, 6, 15))
    # B's wall cuts at 2025-06-15; A keeps serving up to the query date
    assert [(s.start, s.end, s.sources) for s in seg] == [
        (date(2020, 1, 1), date(2025, 6, 15), ("A", "B")),
        (date(2025, 6, 16), date(2026, 6, 15), ("A",)),
    ]
