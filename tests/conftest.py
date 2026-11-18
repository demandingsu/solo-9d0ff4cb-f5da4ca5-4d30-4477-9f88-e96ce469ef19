import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_package(client, name, lic_start="2020-01-01", lic_end="2030-12-31"):
    r = client.post(
        "/packages",
        json={"name": name, "license_start": lic_start, "license_end": lic_end},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def put_coverage(client, pkg_id, entries):
    return client.put(f"/packages/{pkg_id}/coverage", json={"entries": entries})


def entry(issn, start, end, embargo=0):
    return {
        "issn": issn,
        "start_date": start,
        "end_date": end,
        "embargo_months": embargo,
    }


def query(client, issn, date):
    r = client.get("/access", params={"issn": issn, "date": date})
    assert r.status_code == 200, r.text
    return r.json()
