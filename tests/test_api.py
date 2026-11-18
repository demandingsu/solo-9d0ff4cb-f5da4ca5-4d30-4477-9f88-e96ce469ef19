"""API integration tests: overlapping coverage, failure rollback, concurrency."""

import threading

from conftest import entry, make_package, put_coverage, query

ISSN = "1234-5678"


def test_embargo_boundary_moves_with_query_date(client):
    pkg = make_package(client, "PackA")
    assert put_coverage(client, pkg, [entry(ISSN, "2018-01-01", "2026-12-31", 6)]).status_code == 200

    r = query(client, ISSN, "2026-11-18")
    assert [(s["start"], s["end"], s["sources"]) for s in r["segments"]] == [
        ("2018-01-01", "2026-05-18", ["PackA"])
    ]
    r = query(client, ISSN, "2026-03-10")
    assert r["segments"][0]["end"] == "2025-09-10"
    # query so early that everything is inside the embargo window
    assert query(client, ISSN, "2018-03-01")["segments"] == []


def test_license_window_enforced(client):
    pkg = make_package(client, "PackA", lic_start="2024-01-01", lic_end="2025-12-31")
    put_coverage(client, pkg, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])

    assert query(client, ISSN, "2023-12-31")["segments"] == []  # before license
    assert query(client, ISSN, "2026-01-01")["segments"] == []   # after license
    assert len(query(client, ISSN, "2025-06-15")["segments"]) == 1


def test_overlap_splits_and_deactivation_keeps_other_source(client):
    a = make_package(client, "PackA")
    b = make_package(client, "PackB")
    put_coverage(client, a, [entry(ISSN, "2015-01-01", "2024-12-31", 0)])
    put_coverage(client, b, [entry(ISSN, "2020-01-01", "2024-12-31", 0)])

    r = query(client, ISSN, "2026-01-01")
    assert [(s["start"], s["end"], s["sources"]) for s in r["segments"]] == [
        ("2015-01-01", "2019-12-31", ["PackA"]),
        ("2020-01-01", "2024-12-31", ["PackA", "PackB"]),
    ]

    # Deactivating A must not lose B's basis for the overlap years.
    assert client.post(f"/packages/{a}/deactivate").status_code == 200
    r = query(client, ISSN, "2026-01-01")
    assert [(s["start"], s["end"], s["sources"]) for s in r["segments"]] == [
        ("2020-01-01", "2024-12-31", ["PackB"])
    ]

    # Reactivating restores the split view.
    assert client.post(f"/packages/{a}/activate").status_code == 200
    assert len(query(client, ISSN, "2026-01-01")["segments"]) == 2


def test_failed_snapshot_keeps_previous_version(client):
    pkg = make_package(client, "PackA")
    put_coverage(client, pkg, [entry(ISSN, "2010-01-01", "2020-12-31", 0)])
    before = query(client, ISSN, "2026-01-01")

    # invalid row (end before start) -> whole snapshot rejected
    bad = put_coverage(client, pkg, [
        entry(ISSN, "2021-01-01", "2025-12-31", 0),
        entry("9999-0001", "2025-01-01", "2024-01-01", 0),
    ])
    assert bad.status_code == 422

    after = query(client, ISSN, "2026-01-01")
    assert after["segments"] == before["segments"]          # old data still served
    assert after["data_version"] == before["data_version"]  # no partial commit

    # invalid ISSN format also rejected atomically
    assert put_coverage(client, pkg, [entry("not-an-issn", "2021-01-01", "2025-12-31")]).status_code == 422
    assert query(client, ISSN, "2026-01-01")["segments"] == before["segments"]


def test_invalid_license_update_rejected_old_window_stays(client):
    pkg = make_package(client, "PackA", lic_start="2024-01-01", lic_end="2025-12-31")
    put_coverage(client, pkg, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])

    r = client.put(f"/packages/{pkg}/license",
                   json={"license_start": "2026-01-01", "license_end": "2025-01-01"})
    assert r.status_code == 422
    # old license still governs
    assert len(query(client, ISSN, "2025-06-15")["segments"]) == 1
    assert query(client, ISSN, "2026-06-15")["segments"] == []


def test_license_update_takes_effect_atomically(client):
    pkg = make_package(client, "PackA", lic_start="2024-01-01", lic_end="2024-12-31")
    put_coverage(client, pkg, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    assert query(client, ISSN, "2026-01-01")["segments"] == []

    v_before = query(client, ISSN, "2024-06-01")["data_version"]
    r = client.put(f"/packages/{pkg}/license",
                   json={"license_start": "2024-01-01", "license_end": "2027-12-31"})
    assert r.status_code == 200
    after = query(client, ISSN, "2026-01-01")
    assert len(after["segments"]) == 1
    assert after["data_version"] > v_before


def test_unknown_package_and_issn_validation(client):
    assert client.get("/packages/999").status_code == 404
    assert client.put("/packages/999/license",
                      json={"license_start": "2024-01-01", "license_end": "2025-01-01"}).status_code == 404
    assert client.get("/access", params={"issn": "junk", "date": "2026-01-01"}).status_code == 422


def test_issn_normalization(client):
    pkg = make_package(client, "PackA")
    put_coverage(client, pkg, [entry("12345678", "2010-01-01", "2020-12-31", 0)])
    r = query(client, "1234-5678", "2026-01-01")
    assert r["issn"] == "1234-5678"
    assert len(r["segments"]) == 1


def test_concurrent_reads_never_mix_versions(client):
    """Readers must always see one fully committed snapshot, old or new."""
    pkg = make_package(client, "PackA")
    put_coverage(client, pkg, [entry(ISSN, "2010-01-01", "2020-12-31", 0)])
    old_version = query(client, ISSN, "2026-01-01")["data_version"]

    errors = []

    def reader():
        try:
            for _ in range(60):
                r = query(client, ISSN, "2026-01-01")
                ends = {s["end"] for s in r["segments"]}
                if r["data_version"] == old_version:
                    assert ends == {"2020-12-31"}, (r["data_version"], ends)
                elif r["data_version"] == old_version + 1:
                    assert ends == {"2015-12-31"}, (r["data_version"], ends)
                else:
                    raise AssertionError(f"unexpected version {r['data_version']}")
        except AssertionError as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    # writer commits the replacement mid-stream
    put_coverage(client, pkg, [entry(ISSN, "2010-01-01", "2015-12-31", 0)])
    for t in threads:
        t.join()

    assert not errors
    assert query(client, ISSN, "2026-01-01")["data_version"] == old_version + 1
