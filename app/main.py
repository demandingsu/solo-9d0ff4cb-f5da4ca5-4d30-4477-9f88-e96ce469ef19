"""FastAPI application: journal coverage snapshots -> access segments and
reader access-attempt verification."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse

from . import db
from . import logic
from .logic import CoverageRow, compute_segments
from .schemas import (
    AccessAttemptCreate,
    AccessAttemptOut,
    AccessResponse,
    AlternativePackageOut,
    CoverageBasisOut,
    CoverageSnapshot,
    LicenseUpdate,
    PackageCreate,
    PackageOut,
    SegmentOut,
    VerificationBasisOut,
    VerificationItem,
    VerificationResponse,
    normalize_issn,
)


# Human-readable (Chinese) explanations for machine reason codes.
REASON_TEXT = {
    logic.REASON_PACKAGE_INACTIVE: "资源包已停用，不参与访问判定",
    logic.REASON_LICENSE_NOT_STARTED: "访问日早于授权起始日，授权尚未生效",
    logic.REASON_LICENSE_EXPIRED: "访问日晚于授权终止日，授权已过期",
    logic.REASON_NO_COVERAGE: "该包未收录此 ISSN 的覆盖记录",
    logic.REASON_PUB_BEFORE_COVERAGE: "文章出版日早于该包覆盖起始日，不在收录范围",
    logic.REASON_PUB_AFTER_COVERAGE: "文章出版日晚于该包覆盖终止日，不在收录范围",
    logic.REASON_EMBARGO: "文章仍在移动禁访墙内（按禁访月数暂不可访）",
}

MATCH_TEXT = {
    logic.MATCH_OK: "实际成功与应有权限一致，无不符",
    logic.MATCH_EXPECTED_DENIAL: "实际拒绝与应有权限一致，无不符",
    logic.MATCH_WRONGFUL_DENIAL: "该包对此文章应有权限，但实际被拒绝",
    logic.MATCH_UNEXPECTED_SUCCESS: "该包对此文章无权限，但实际访问成功",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Library Journal Access API",
    version="2.0.0",
    description=(
        "Stores per-package journal coverage snapshots (ISSN, publication "
        "date range, embargo months) and package license windows, then "
        "answers, for a query date + ISSN, which publication date ranges "
        "are accessible and from which packages.  Also records reader "
        "access attempts by request number and re-verifies each attempt "
        "against the currently committed entitlements: expected access, "
        "actual outcome, mismatch reason, alternative packages, and "
        "library-wide loss of access."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------- helpers

def _package_or_404(conn: sqlite3.Connection, package_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM packages WHERE id = ?", (package_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"package {package_id} not found")
    return row


def _package_out(conn: sqlite3.Connection, row: sqlite3.Row) -> PackageOut:
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM coverage WHERE package_id = ?", (row["id"],)
    ).fetchone()["n"]
    return PackageOut(
        id=row["id"],
        name=row["name"],
        license_start=date.fromisoformat(row["license_start"]),
        license_end=date.fromisoformat(row["license_end"]),
        active=bool(row["active"]),
        coverage_rows=count,
    )


# ---------------------------------------------------------------- routes

@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/packages", response_model=PackageOut, status_code=201)
def create_package(body: PackageCreate) -> PackageOut:
    """Register a package with its license start/end dates."""
    with db.write_tx() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO packages (name, license_start, license_end) VALUES (?, ?, ?)",
                (body.name, body.license_start.isoformat(), body.license_end.isoformat()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"package name {body.name!r} already exists")
        db.bump_version(conn)
        return _package_out(conn, _package_or_404(conn, cur.lastrowid))


@app.get("/packages", response_model=List[PackageOut])
def list_packages() -> List[PackageOut]:
    with db.read_tx() as conn:
        rows = conn.execute("SELECT * FROM packages ORDER BY id").fetchall()
        return [_package_out(conn, r) for r in rows]


@app.get("/packages/{package_id}", response_model=PackageOut)
def get_package(package_id: int) -> PackageOut:
    with db.read_tx() as conn:
        return _package_out(conn, _package_or_404(conn, package_id))


@app.put("/packages/{package_id}/license", response_model=PackageOut)
def update_license(package_id: int, body: LicenseUpdate) -> PackageOut:
    """Replace a package's license window (single committed transaction)."""
    with db.write_tx() as conn:
        _package_or_404(conn, package_id)
        conn.execute(
            "UPDATE packages SET license_start = ?, license_end = ? WHERE id = ?",
            (body.license_start.isoformat(), body.license_end.isoformat(), package_id),
        )
        db.bump_version(conn)
        return _package_out(conn, _package_or_404(conn, package_id))


@app.put("/packages/{package_id}/coverage", response_model=PackageOut)
def replace_coverage(package_id: int, body: CoverageSnapshot) -> PackageOut:
    """Atomically replace the package's full coverage snapshot.

    The old snapshot stays in place until the new one commits; any
    failure rolls back to the previous committed version.
    """
    with db.write_tx() as conn:
        _package_or_404(conn, package_id)
        conn.execute("DELETE FROM coverage WHERE package_id = ?", (package_id,))
        conn.executemany(
            "INSERT INTO coverage (package_id, issn, start_date, end_date, embargo_months)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (package_id, e.issn, e.start_date.isoformat(), e.end_date.isoformat(), e.embargo_months)
                for e in body.entries
            ],
        )
        db.bump_version(conn)
        return _package_out(conn, _package_or_404(conn, package_id))


@app.get("/packages/{package_id}/coverage")
def get_coverage(package_id: int) -> dict:
    with db.read_tx() as conn:
        _package_or_404(conn, package_id)
        rows = conn.execute(
            "SELECT issn, start_date, end_date, embargo_months FROM coverage"
            " WHERE package_id = ? ORDER BY issn, start_date",
            (package_id,),
        ).fetchall()
        return {
            "package_id": package_id,
            "data_version": db.current_version(conn),
            "entries": [dict(r) for r in rows],
        }


@app.post("/packages/{package_id}/deactivate", response_model=PackageOut)
def deactivate_package(package_id: int) -> PackageOut:
    """Stop using a package in answers; its rows stay stored and other
    packages' coverage is untouched."""
    with db.write_tx() as conn:
        _package_or_404(conn, package_id)
        conn.execute("UPDATE packages SET active = 0 WHERE id = ?", (package_id,))
        db.bump_version(conn)
        return _package_out(conn, _package_or_404(conn, package_id))


@app.post("/packages/{package_id}/activate", response_model=PackageOut)
def activate_package(package_id: int) -> PackageOut:
    with db.write_tx() as conn:
        _package_or_404(conn, package_id)
        conn.execute("UPDATE packages SET active = 1 WHERE id = ?", (package_id,))
        db.bump_version(conn)
        return _package_out(conn, _package_or_404(conn, package_id))


@app.get("/access", response_model=AccessResponse)
def access(
    issn: str = Query(..., description="Journal ISSN, e.g. 1234-5678"),
    query_date: date = Query(..., alias="date", description="Query date, YYYY-MM-DD"),
) -> AccessResponse:
    """Accessible publication date segments for an ISSN on a query date.

    Only packages licensed *and* active on the query date contribute;
    each package's embargo moves with the query date.  Segments are
    split wherever the set of source packages changes.
    """
    try:
        norm_issn = normalize_issn(issn)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    with db.read_tx() as conn:
        version = db.current_version(conn)
        rows = conn.execute(
            """
            SELECT p.name AS package, c.start_date, c.end_date, c.embargo_months
            FROM coverage c
            JOIN packages p ON p.id = c.package_id
            WHERE c.issn = ?
              AND p.active = 1
              AND p.license_start <= ?
              AND p.license_end   >= ?
            """,
            (norm_issn, query_date.isoformat(), query_date.isoformat()),
        ).fetchall()

    coverage = [
        CoverageRow(
            package=r["package"],
            start=date.fromisoformat(r["start_date"]),
            end=date.fromisoformat(r["end_date"]),
            embargo_months=r["embargo_months"],
        )
        for r in rows
    ]
    segments = compute_segments(coverage, query_date)
    return AccessResponse(
        issn=norm_issn,
        date=query_date,
        data_version=version,
        segments=[
            SegmentOut(start=s.start, end=s.end, sources=list(s.sources)) for s in segments
        ],
    )


# ------------------------------------------------- access-attempt helpers

def _attempt_out(row: sqlite3.Row, version: int, duplicate: bool = False) -> AccessAttemptOut:
    return AccessAttemptOut(
        request_no=row["request_no"],
        access_date=date.fromisoformat(row["access_date"]),
        issn=row["issn"],
        article_pub_date=date.fromisoformat(row["article_pub_date"]),
        package_id=row["package_id"],
        outcome=row["outcome"],
        created_at=row["created_at"],
        created_version=row["created_version"],
        data_version=version,
        duplicate=duplicate,
    )


def _load_package_states(
    conn: sqlite3.Connection, issn: str, article_pub_date: date, access_date: date
) -> dict:
    """Load every package plus its coverage rows for one ISSN, inside the
    caller's (single) read transaction, so verdicts and alternatives all
    rest on the same committed snapshot."""
    pkg_rows = conn.execute("SELECT * FROM packages ORDER BY id").fetchall()
    cov_rows = conn.execute(
        "SELECT package_id, start_date, end_date, embargo_months FROM coverage"
        " WHERE issn = ? ORDER BY package_id, start_date, end_date",
        (issn,),
    ).fetchall()
    by_pkg: dict = {p["id"]: [] for p in pkg_rows}
    for c in cov_rows:
        by_pkg.setdefault(c["package_id"], []).append(
            (date.fromisoformat(c["start_date"]),
             date.fromisoformat(c["end_date"]),
             c["embargo_months"])
        )
    states = {}
    for p in pkg_rows:
        basis = logic.build_coverage_basis(
            by_pkg.get(p["id"], []), article_pub_date, access_date
        )
        states[p["id"]] = logic.PackageState(
            package_id=p["id"],
            name=p["name"],
            license_start=date.fromisoformat(p["license_start"]),
            license_end=date.fromisoformat(p["license_end"]),
            active=bool(p["active"]),
            coverage=basis,
        )
    return states


def _basis_out(verdict: logic.PackageVerdict, access_date: date) -> VerificationBasisOut:
    matched = verdict.matched_row
    return VerificationBasisOut(
        package_id=verdict.package_id,
        package_name=verdict.name,
        active=verdict.active,
        license_start=verdict.license_start,
        license_end=verdict.license_end,
        license_covers_access_date=(
            verdict.license_start <= access_date <= verdict.license_end
        ),
        coverage_rows=[
            CoverageBasisOut(
                start=b.start, end=b.end, embargo_months=b.embargo_months,
                embargo_cutoff=b.embargo_cutoff,
                covers_article=b.covers_article, embargo_blocks=b.embargo_blocks,
            ) for b in verdict.basis
        ],
        matched_coverage=(
            CoverageBasisOut(
                start=matched.start, end=matched.end,
                embargo_months=matched.embargo_months,
                embargo_cutoff=matched.embargo_cutoff,
                covers_article=matched.covers_article,
                embargo_blocks=matched.embargo_blocks,
            ) if matched is not None else None
        ),
    )


# ------------------------------------------------------------- routes (v2)

# Fields that define the content of an attempt; a retransmit with the
# same request_no must match all of these to be accepted as a duplicate.
_ATTEMPT_FIELDS = ("access_date", "issn", "article_pub_date", "package_id", "outcome")


@app.post("/access-attempts", response_model=AccessAttemptOut, status_code=201)
def record_access_attempt(body: AccessAttemptCreate) -> AccessAttemptOut:
    """Record one reader access attempt, keyed by request number.

    Idempotency: retransmitting the same request_no with identical
    content is accepted but stored only once (``duplicate: true``).  A
    retransmit whose content differs is rejected with 409 and the list
    of differing fields, leaving the original record untouched.
    """
    with db.write_tx() as conn:
        existing = conn.execute(
            "SELECT * FROM access_attempts WHERE request_no = ?", (body.request_no,)
        ).fetchone()
        if existing is not None:
            incoming = {
                "access_date": body.access_date.isoformat(),
                "issn": body.issn,
                "article_pub_date": body.article_pub_date.isoformat(),
                "package_id": body.package_id,
                "outcome": body.outcome,
            }
            differing = [f for f in _ATTEMPT_FIELDS if existing[f] != incoming[f]]
            if differing:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "request_no already exists with different content; "
                                   "retransmit rejected",
                        "request_no": body.request_no,
                        "differing_fields": differing,
                    },
                )
            # Identical retransmit: record only once, no new version.
            return _attempt_out(existing, db.current_version(conn), duplicate=True)

        _package_or_404(conn, body.package_id)
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Bump the version first, then stamp the insert with the version
        # that contains it; both happen inside the same transaction.
        new_version = db.bump_version(conn)
        cur = conn.execute(
            "INSERT INTO access_attempts (request_no, access_date, issn,"
            " article_pub_date, package_id, outcome, created_at, created_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (body.request_no, body.access_date.isoformat(), body.issn,
             body.article_pub_date.isoformat(), body.package_id, body.outcome,
             created_at, new_version),
        )
        row = conn.execute(
            "SELECT * FROM access_attempts WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _attempt_out(row, new_version)


@app.get("/access-attempts", response_model=List[AccessAttemptOut])
def list_access_attempts(
    start_date: Optional[date] = Query(None, description="Inclusive lower bound on access_date"),
    end_date: Optional[date] = Query(None, description="Inclusive upper bound on access_date"),
    request_no: Optional[str] = Query(None),
) -> List[AccessAttemptOut]:
    """Raw stored attempts (verification is recomputed in /verifications)."""
    if start_date and end_date and end_date < start_date:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date")
    sql = "SELECT * FROM access_attempts WHERE 1=1"
    params: list = []
    if start_date is not None:
        sql += " AND access_date >= ?"
        params.append(start_date.isoformat())
    if end_date is not None:
        sql += " AND access_date <= ?"
        params.append(end_date.isoformat())
    if request_no is not None:
        sql += " AND request_no = ?"
        params.append(request_no)
    sql += " ORDER BY access_date, id"
    with db.read_tx() as conn:
        version = db.current_version(conn)
        rows = conn.execute(sql, params).fetchall()
        return [_attempt_out(r, version) for r in rows]


@app.get("/verifications", response_model=VerificationResponse)
def verify_access(
    start_date: date = Query(..., description="Inclusive lower bound on access_date"),
    end_date: date = Query(..., description="Inclusive upper bound on access_date"),
) -> VerificationResponse:
    """Re-verify every attempt in an access-date window.

    Each attempt is judged against the *currently committed*
    entitlements inside one read transaction, so the returned records,
    their bases (license window, coverage rows, embargo cutoffs) and
    conclusions all come from the same ``data_version``.  Because
    verdicts are recomputed on demand, retroactive coverage/license
    corrections and late backfilled records are reflected automatically.
    A single package's refusal is never treated as library-wide loss
    while another package can serve the article.
    """
    if end_date < start_date:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date")

    with db.read_tx() as conn:
        version = db.current_version(conn)
        attempts = conn.execute(
            "SELECT * FROM access_attempts"
            " WHERE access_date >= ? AND access_date <= ?"
            " ORDER BY access_date, id",
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()

        items: List[VerificationItem] = []
        for a in attempts:
            access_date = date.fromisoformat(a["access_date"])
            article_pub_date = date.fromisoformat(a["article_pub_date"])
            states = _load_package_states(
                conn, a["issn"], article_pub_date, access_date
            )

            attempted = states[a["package_id"]]
            verdict = logic.evaluate_package(attempted, article_pub_date, access_date)

            # Alternatives: *other* packages that should serve the article
            # on the same access date, ordered by package id.
            alternatives: List[AlternativePackageOut] = []
            for pid in sorted(states):
                if pid == a["package_id"]:
                    continue
                other = logic.evaluate_package(states[pid], article_pub_date, access_date)
                if other.expected_access:
                    alternatives.append(AlternativePackageOut(
                        package_id=other.package_id,
                        name=other.name,
                        matched_coverage=(
                            CoverageBasisOut(
                                start=other.matched_row.start,
                                end=other.matched_row.end,
                                embargo_months=other.matched_row.embargo_months,
                                embargo_cutoff=other.matched_row.embargo_cutoff,
                                covers_article=other.matched_row.covers_article,
                                embargo_blocks=other.matched_row.embargo_blocks,
                            ) if other.matched_row is not None else None
                        ),
                    ))
            alt_ids = tuple(x.package_id for x in alternatives)

            match = logic.classify_match(a["outcome"], verdict.expected_access)
            mismatch = None if match in (logic.MATCH_OK, logic.MATCH_EXPECTED_DENIAL) else match
            wide_loss = logic.library_wide_loss(
                a["outcome"], verdict.expected_access, alt_ids
            )

            items.append(VerificationItem(
                request_no=a["request_no"],
                access_date=access_date,
                issn=a["issn"],
                article_pub_date=article_pub_date,
                package_id=attempted.package_id,
                package_name=attempted.name,
                observed_outcome=a["outcome"],
                expected_access=verdict.expected_access,
                expected_reason=verdict.reason,
                expected_reason_text=(REASON_TEXT[verdict.reason] if verdict.reason else None),
                match_result=match,
                mismatch_reason=mismatch,
                mismatch_reason_text=(MATCH_TEXT[mismatch] if mismatch else None),
                alternative_packages=alternatives,
                library_wide_access_loss=wide_loss,
                basis=_basis_out(verdict, access_date),
            ))

        return VerificationResponse(
            data_version=version,
            start_date=start_date,
            end_date=end_date,
            count=len(items),
            items=items,
        )
