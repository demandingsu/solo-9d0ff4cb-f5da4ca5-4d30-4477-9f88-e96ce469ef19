"""Unit tests for the pure segment/moving-wall and verification logic."""

from datetime import date

from app.logic import (
    CoverageRow,
    PackageState,
    build_coverage_basis,
    classify_match,
    compute_segments,
    evaluate_package,
    library_wide_loss,
    subtract_months,
)


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


# ----------------------------------------------------- verification logic

def _state(pid=1, name="A", start=date(2024, 1, 1), end=date(2027, 12, 31),
           active=True, rows=()):
    basis = build_coverage_basis(rows, date(2026, 3, 1), date(2026, 11, 10))
    return PackageState(pid, name, start, end, active, basis)


def test_evaluate_package_entitled():
    st = _state(rows=[(date(2010, 1, 1), date(2030, 12, 31), 0)])
    v = evaluate_package(st, date(2026, 3, 1), date(2026, 11, 10))
    assert v.expected_access is True and v.reason is None
    assert v.matched_row.covers_article is True


def test_evaluate_package_reasons_in_order():
    v = evaluate_package(_state(active=False), date(2026, 3, 1), date(2026, 11, 10))
    assert v.reason == "package_inactive"
    v = evaluate_package(_state(start=date(2030, 1, 1)), date(2026, 3, 1), date(2026, 11, 10))
    assert v.reason == "license_not_started"
    v = evaluate_package(_state(end=date(2025, 1, 1)), date(2026, 3, 1), date(2026, 11, 10))
    assert v.reason == "license_expired"
    v = evaluate_package(_state(rows=[]), date(2026, 3, 1), date(2026, 11, 10))
    assert v.reason == "no_coverage_for_issn"

    st = _state(rows=[(date(2010, 1, 1), date(2020, 12, 31), 0)])
    v = evaluate_package(st, date(2026, 3, 1), date(2026, 11, 10))
    assert v.reason == "article_after_coverage_end"
    st = _state(rows=[(date(2027, 1, 1), date(2030, 12, 31), 0)])
    v = evaluate_package(st, date(2026, 3, 1), date(2026, 11, 10))
    assert v.reason == "article_before_coverage_start"


def _state(pid=1, name="A", start=date(2024, 1, 1), end=date(2027, 12, 31),
           active=True, rows=(), article=date(2026, 3, 1),
           access=date(2026, 11, 10)):
    basis = build_coverage_basis(rows, article, access)
    return PackageState(pid, name, start, end, active, basis)


def test_evaluate_package_gap_between_non_overlapping_rows():
    rows = [
        (date(2010, 1, 1), date(2015, 12, 31), 0),
        (date(2020, 1, 1), date(2030, 12, 31), 0),
    ]
    # article in the gap between the two windows
    st = _state(rows=rows, article=date(2018, 6, 1))
    v = evaluate_package(st, date(2018, 6, 1), date(2026, 11, 10))
    assert v.reason == "article_after_coverage_end"
    # before the earliest window still reports the before reason
    st = _state(rows=rows, article=date(2009, 6, 1))
    v = evaluate_package(st, date(2009, 6, 1), date(2026, 11, 10))
    assert v.reason == "article_before_coverage_start"
    # a date in the later window is served
    st = _state(rows=rows, article=date(2022, 6, 1))
    v = evaluate_package(st, date(2022, 6, 1), date(2026, 11, 10))
    assert v.expected_access is True


def test_evaluate_package_embargo_and_short_embargo_row_wins():
    # one row blocks by embargo, another row with a shorter wall serves it
    st = _state(rows=[
        (date(2010, 1, 1), date(2030, 12, 31), 12),
        (date(2010, 1, 1), date(2030, 12, 31), 0),
    ])
    v = evaluate_package(st, date(2026, 3, 1), date(2026, 11, 10))
    assert v.expected_access is True  # the 0-month row grants access

    st = _state(rows=[(date(2010, 1, 1), date(2030, 12, 31), 12)])
    v = evaluate_package(st, date(2026, 3, 1), date(2026, 11, 10))
    assert v.reason == "embargo_blocks_article"
    assert v.matched_row.embargo_cutoff == date(2025, 11, 10)


def test_classify_match_table():
    assert classify_match("success", True) == "consistent"
    assert classify_match("refused", True) == "wrongful_denial"
    assert classify_match("success", False) == "unexpected_success"
    assert classify_match("refused", False) == "expected_denial"


def test_library_wide_loss_rules():
    # refused but another package can serve -> single-package problem, not wide loss
    assert library_wide_loss("refused", True, (2,)) is False
    assert library_wide_loss("refused", False, (2,)) is False
    # wrongful refusal (entitled) with no alternative -> real library-wide loss
    assert library_wide_loss("refused", True, ()) is True
    # expected denial (no entitlement anywhere) -> not "loss", just not licensed
    assert library_wide_loss("refused", False, ()) is False
    # a success never counts as loss even if unexpected
    assert library_wide_loss("success", False, ()) is False
    assert library_wide_loss("success", True, ()) is False
