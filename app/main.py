"""FastAPI application: journal coverage snapshots -> access segments."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, List

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse

from . import db
from .logic import CoverageRow, compute_segments
from .schemas import (
    AccessResponse,
    CoverageSnapshot,
    LicenseUpdate,
    PackageCreate,
    PackageOut,
    SegmentOut,
    normalize_issn,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Library Journal Access API",
    version="1.0.0",
    description=(
        "Stores per-package journal coverage snapshots (ISSN, publication "
        "date range, embargo months) and package license windows, then "
        "answers, for a query date + ISSN, which publication date ranges "
        "are accessible and from which packages."
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
