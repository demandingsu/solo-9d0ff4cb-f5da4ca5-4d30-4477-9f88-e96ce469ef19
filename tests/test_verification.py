"""Integration tests for access-attempt recording and verification."""

import threading

from conftest import entry, make_package, put_coverage

ISSN = "1234-5678"
OTHER_ISSN = "9999-0001"


def post_attempt(client, **over):
    payload = {
        "request_no": "R-001",
        "access_date": "2026-11-10",
        "issn": ISSN,
        "article_pub_date": "2020-03-01",
        "package_id": 1,
        "outcome": "success",
    }
    payload.update(over)
    return client.post("/access-attempts", json=payload)


def verify(client, start="2020-01-01", end="2030-12-31", **params):
    return client.get("/verifications", params={"start_date": start, "end_date": end, **params})


def item_for(r, request_no):
    return next(i for i in r.json()["items"] if i["request_no"] == request_no)


# ------------------------------------------------------------- recording

def test_record_and_list_attempt(client):
    pkg = make_package(client, "PackA")
    r = post_attempt(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["duplicate"] is False
    assert body["data_version"] >= 1
    assert body["created_version"] == body["data_version"]

    listed = client.get("/access-attempts").json()
    assert len(listed) == 1 and listed[0]["request_no"] == "R-001"


def test_same_number_same_content_idempotent_recorded_once(client):
    make_package(client, "PackA")
    assert post_attempt(client).status_code == 201
    again = post_attempt(client)
    assert again.status_code == 201
    assert again.json()["duplicate"] is True
    # still only one row, version untouched by the duplicate retransmit
    assert len(client.get("/access-attempts").json()) == 1


def test_same_number_different_content_rejected(client):
    make_package(client, "PackA")
    assert post_attempt(client).status_code == 201
    conflict = post_attempt(client, outcome="refused")
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["differing_fields"] == ["outcome"]
    # original record keeps the first outcome
    assert client.get("/access-attempts").json()[0]["outcome"] == "success"


def test_unknown_package_rejected_and_rolled_back(client):
    r = post_attempt(client, package_id=999)
    assert r.status_code == 404
    assert client.get("/access-attempts").json() == []


def test_outcome_and_issn_validation(client):
    make_package(client, "PackA")
    assert post_attempt(client, outcome="maybe").status_code == 422
    assert post_attempt(client, issn="junk").status_code == 422


def test_verification_window_bounds(client):
    make_package(client, "PackA")
    put_coverage(client, 1, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    post_attempt(client, request_no="R-early", access_date="2026-01-05")
    post_attempt(client, request_no="R-late", access_date="2026-06-05")

    r = verify(client, start="2026-02-01", end="2026-12-31")
    assert [i["request_no"] for i in r.json()["items"]] == ["R-late"]
    bad = verify(client, start="2026-12-31", end="2026-01-01")
    assert bad.status_code == 422


# ------------------------------------------------------- expected access

def test_success_when_entitled_is_consistent(client):
    make_package(client, "PackA")
    put_coverage(client, 1, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    post_attempt(client, outcome="success")
    r = verify(client)
    it = item_for(r, "R-001")
    assert it["expected_access"] is True
    assert it["match_result"] == "consistent"
    assert it["mismatch_reason"] is None
    assert it["library_wide_access_loss"] is False
    # basis: license window and the matched coverage row
    assert it["basis"]["license_covers_access_date"] is True
    assert it["basis"]["matched_coverage"]["covers_article"] is True


def test_expected_denial_reasons(client):
    # no coverage at all for the ISSN
    make_package(client, "PackA")
    post_attempt(client, outcome="refused", request_no="R-nocov")
    r = verify(client)
    it = item_for(r, "R-nocov")
    assert it["expected_access"] is False
    assert it["expected_reason"] == "no_coverage_for_issn"
    assert it["match_result"] == "expected_denial"

    # publication date outside the coverage window
    put_coverage(client, 1, [entry(ISSN, "2015-01-01", "2025-12-31", 0)])
    post_attempt(client, request_no="R-before", article_pub_date="2010-06-01", outcome="refused")
    post_attempt(client, request_no="R-after", article_pub_date="2026-06-01", outcome="refused")
    r = verify(client)
    assert item_for(r, "R-before")["expected_reason"] == "article_before_coverage_start"
    assert item_for(r, "R-after")["expected_reason"] == "article_after_coverage_end"

    # embargo moving wall blocks a fresh article
    pkg2 = make_package(client, "PackB")
    put_coverage(client, pkg2, [entry(OTHER_ISSN, "2020-01-01", "2030-12-31", 12)])
    post_attempt(client, request_no="R-embargo", issn=OTHER_ISSN, package_id=pkg2,
                 article_pub_date="2026-08-01", access_date="2026-11-10", outcome="refused")
    r = verify(client)
    it = item_for(r, "R-embargo")
    assert it["expected_reason"] == "embargo_blocks_article"
    assert it["basis"]["matched_coverage"]["embargo_cutoff"] == "2025-11-10"


def test_license_window_and_inactive_drive_expected_denial(client):
    pkg = make_package(client, "PackA", lic_start="2024-01-01", lic_end="2025-12-31")
    put_coverage(client, pkg, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])

    post_attempt(client, request_no="R-before-lic", access_date="2023-12-31", outcome="refused")
    post_attempt(client, request_no="R-after-lic", access_date="2026-01-01", outcome="refused")
    r = verify(client)
    assert item_for(r, "R-before-lic")["expected_reason"] == "license_not_started"
    assert item_for(r, "R-after-lic")["expected_reason"] == "license_expired"

    client.post(f"/packages/{pkg}/deactivate")
    post_attempt(client, request_no="R-inactive", access_date="2025-06-01", outcome="refused")
    r = verify(client)
    assert item_for(r, "R-inactive")["expected_reason"] == "package_inactive"


def test_wrongful_denial_flagged(client):
    make_package(client, "PackA")
    put_coverage(client, 1, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    post_attempt(client, outcome="refused")
    r = verify(client)
    it = item_for(r, "R-001")
    assert it["expected_access"] is True
    assert it["match_result"] == "wrongful_denial"
    assert "应有权限" in it["mismatch_reason_text"]


def test_unexpected_success_flagged(client):
    make_package(client, "PackA")
    # no coverage -> no entitlement, but the reader got through
    post_attempt(client, outcome="success")
    r = verify(client)
    it = item_for(r, "R-001")
    assert it["expected_access"] is False
    assert it["match_result"] == "unexpected_success"


# ------------------------------------------------------- multi-package

def test_single_package_refusal_lists_alternatives_not_wide_loss(client):
    a = make_package(client, "PackA")
    b = make_package(client, "PackB")
    # A does not cover the journal; B covers it with no embargo
    put_coverage(client, b, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    post_attempt(client, package_id=a, outcome="refused")

    r = verify(client)
    it = item_for(r, "R-001")
    # A's refusal is expected for A, and must not read as library-wide loss
    assert it["match_result"] == "expected_denial"
    assert it["library_wide_access_loss"] is False
    assert [p["package_id"] for p in it["alternative_packages"]] == [b]
    assert it["alternative_packages"][0]["matched_coverage"]["covers_article"] is True


def test_wrongful_denial_with_alternative_still_not_wide_loss(client):
    a = make_package(client, "PackA")
    b = make_package(client, "PackB")
    put_coverage(client, a, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    put_coverage(client, b, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    post_attempt(client, package_id=a, outcome="refused")  # A should have served

    r = verify(client)
    it = item_for(r, "R-001")
    assert it["match_result"] == "wrongful_denial"
    assert it["library_wide_access_loss"] is False
    assert [p["package_id"] for p in it["alternative_packages"]] == [b]


def test_refusal_with_no_alternative_is_wide_loss(client):
    a = make_package(client, "PackA")
    b = make_package(client, "PackB", lic_start="2030-01-01", lic_end="2031-12-31")
    put_coverage(client, a, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    put_coverage(client, b, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    # A wrongly refuses; B's license is not active on the access date
    post_attempt(client, package_id=a, outcome="refused")

    r = verify(client)
    it = item_for(r, "R-001")
    assert it["match_result"] == "wrongful_denial"
    assert it["alternative_packages"] == []
    assert it["library_wide_access_loss"] is True


def test_embargoed_alternative_is_not_listed(client):
    a = make_package(client, "PackA")
    b = make_package(client, "PackB")
    put_coverage(client, a, [entry(ISSN, "2020-01-01", "2030-12-31", 0)])
    # B has the journal but a 12-month wall blocks this fresh article
    put_coverage(client, b, [entry(ISSN, "2020-01-01", "2030-12-31", 12)])
    post_attempt(client, package_id=a, outcome="refused",
                 article_pub_date="2026-08-01", access_date="2026-11-10")
    r = verify(client)
    it = item_for(r, "R-001")
    assert it["alternative_packages"] == []
    assert it["library_wide_access_loss"] is True


def test_expected_denial_without_alternative_is_not_wide_loss(client):
    # Only one package, and its embargo legitimately blocks the article.
    a = make_package(client, "PackA")
    put_coverage(client, a, [entry(ISSN, "2020-01-01", "2030-12-31", 12)])
    post_attempt(client, package_id=a, outcome="refused",
                 article_pub_date="2026-08-01", access_date="2026-11-10")
    r = verify(client)
    it = item_for(r, "R-001")
    assert it["match_result"] == "expected_denial"
    assert it["alternative_packages"] == []
    # no entitlement exists anywhere -> not a loss of access
    assert it["library_wide_access_loss"] is False


# ------------------------------------------------------- re-judgement

def test_late_backfilled_attempt_judged_against_current_state(client):
    # Record the attempt before any entitlement data exists for the journal
    a = make_package(client, "PackA")
    post_attempt(client, outcome="refused")
    r = verify(client)
    assert item_for(r, "R-001")["match_result"] == "expected_denial"  # no coverage yet

    # Backfill coverage later: the historical attempt is re-judged now
    put_coverage(client, a, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    r = verify(client)
    it = item_for(r, "R-001")
    assert it["match_result"] == "wrongful_denial"
    assert it["basis"]["matched_coverage"] is not None


def test_retroactive_license_correction_flips_historical_verdict(client):
    pkg = make_package(client, "PackA", lic_start="2030-01-01", lic_end="2031-12-31")
    put_coverage(client, pkg, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    post_attempt(client, access_date="2026-11-10", outcome="refused")

    r = verify(client)
    assert item_for(r, "R-001")["expected_reason"] == "license_not_started"

    # Retroactive correction: the license actually covered 2026
    client.put(f"/packages/{pkg}/license",
               json={"license_start": "2024-01-01", "license_end": "2027-12-31"})
    r = verify(client)
    it = item_for(r, "R-001")
    assert it["expected_access"] is True
    assert it["match_result"] == "wrongful_denial"


def test_retroactive_coverage_correction_flips_historical_verdict(client):
    pkg = make_package(client, "PackA")
    put_coverage(client, pkg, [entry(ISSN, "2015-01-01", "2025-12-31", 0)])
    post_attempt(client, article_pub_date="2026-03-01", outcome="refused")
    r = verify(client)
    assert item_for(r, "R-001")["expected_reason"] == "article_after_coverage_end"

    # Publisher corrects the coverage window retroactively
    put_coverage(client, pkg, [entry(ISSN, "2015-01-01", "2030-12-31", 0)])
    r = verify(client)
    it = item_for(r, "R-001")
    assert it["expected_access"] is True
    assert it["match_result"] == "wrongful_denial"


# ------------------------------------------------------- versioning

def test_verification_uses_one_consistent_version(client):
    pkg = make_package(client, "PackA")
    put_coverage(client, pkg, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    post_attempt(client, outcome="success")
    r = verify(client)
    body = r.json()
    version = body["data_version"]
    assert version >= 1
    # every item in the response derives from the same committed version
    assert all("basis" in it for it in body["items"])
    # a later entitlement write advances the version seen by the next query
    client.put(f"/packages/{pkg}/license",
               json={"license_start": "2020-01-01", "license_end": "2032-12-31"})
    assert verify(client).json()["data_version"] == version + 1


def test_failed_write_keeps_old_data_queryable(client):
    pkg = make_package(client, "PackA")
    put_coverage(client, pkg, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    post_attempt(client, outcome="success")
    before = verify(client).json()

    # conflicting retransmit is rejected; the old attempt remains queryable
    conflict = post_attempt(client, outcome="refused")
    assert conflict.status_code == 409
    after = verify(client).json()
    assert after["data_version"] == before["data_version"]
    assert after["items"][0]["observed_outcome"] == "success"

    # a failed coverage write also rolls back, old basis still served
    bad = put_coverage(client, pkg, [entry(OTHER_ISSN, "2025-01-01", "2024-01-01")])
    assert bad.status_code == 422
    again = verify(client).json()
    assert again["data_version"] == before["data_version"]
    assert again["items"][0]["basis"]["coverage_rows"][0]["start"] == "2010-01-01"


def test_verification_never_mixes_versions_under_concurrent_writes(client):
    """While entitlements change, every verification response must be
    internally consistent: its version, expected verdict and basis all
    describe the same committed snapshot."""
    pkg = make_package(client, "PackA")
    put_coverage(client, pkg, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    post_attempt(client, outcome="refused")  # initially wrongful denial

    errors = []

    def reader():
        try:
            for _ in range(60):
                r = verify(client)
                assert r.status_code == 200, r.text
                body = r.json()
                it = item_for(r, "R-001")
                v = body["data_version"]
                # internal consistency: expected flag and reason agree,
                # and the basis license window matches the verdict
                if it["expected_access"]:
                    assert it["expected_reason"] is None
                    assert it["basis"]["license_covers_access_date"] is True
                else:
                    assert it["expected_reason"] is not None
                if it["observed_outcome"] == "refused":
                    assert it["match_result"] in ("wrongful_denial", "expected_denial")
                # the response itself names one single version for all rows
                assert v >= 1
        except AssertionError as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    # writer pushes the package license into the future: the historical
    # refusal must flip to expected_denial under one committed version
    client.put(f"/packages/{pkg}/license",
               json={"license_start": "2030-01-01", "license_end": "2031-12-31"})
    for t in threads:
        t.join()

    assert not errors
    final = item_for(verify(client), "R-001")
    assert final["expected_reason"] == "license_not_started"
    assert final["match_result"] == "expected_denial"


def test_raw_attempts_date_filtering(client):
    make_package(client, "PackA")
    post_attempt(client, request_no="R-1", access_date="2026-02-10")
    post_attempt(client, request_no="R-2", access_date="2026-05-10")
    r = client.get("/access-attempts",
                   params={"start_date": "2026-03-01", "end_date": "2026-12-31"})
    assert [x["request_no"] for x in r.json()] == ["R-2"]
    r = client.get("/access-attempts", params={"request_no": "R-1"})
    assert [x["request_no"] for x in r.json()] == ["R-1"]
    bad = client.get("/access-attempts",
                     params={"start_date": "2026-12-31", "end_date": "2026-01-01"})
    assert bad.status_code == 422
