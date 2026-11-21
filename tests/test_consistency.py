"""Consistency of the editor status read under concurrent writes.

The status endpoint must return one detail snapshot together with the masking,
analysis, version list and bulletin that belong to it.  A concurrent detail
submission or publish is either fully visible or not visible at all, and a
status read must never block writers of this or any other quarter.
"""

from __future__ import annotations

import json
import sqlite3
import threading

from app.db import connect
from app.models import DetailSubmission

Q = "2024Q1"
OTHER = "2024Q2"

DISTRICTS = ["东城区", "西城区", "南城区", "北城区"]
INDUSTRIES = ["制造业", "建筑业", "批发零售"]

VALUES = [
    [30, 50, 40],
    [25, 35, 35],
    [10, 20, 30],
    [45, 3, 2],
]
# Same row and column margins as VALUES, different cells.
ALT = [
    [29, 51, 40],
    [26, 34, 35],
    [10, 20, 30],
    [45, 3, 2],
]
ROW_TOTALS = [sum(r) for r in VALUES]
COL_TOTALS = [sum(VALUES[i][j] for i in range(len(VALUES)))
              for j in range(len(VALUES[0]))]


def register(client, quarter=Q):
    body = {
        "districts": [{"name": n, "total": t}
                      for n, t in zip(DISTRICTS, ROW_TOTALS)],
        "industries": [{"name": n, "total": t}
                       for n, t in zip(INDUSTRIES, COL_TOTALS)],
    }
    response = client.put(f"/api/v1/editor/quarters/{quarter}/totals", json=body)
    assert response.status_code == 200, response.text


def advice_cells(client, quarter=Q):
    response = client.post(
        f"/api/v1/editor/quarters/{quarter}/suppression-advice", json={}
    )
    assert response.status_code == 200, response.text
    return [(c["row"], c["col"]) for c in response.json()["masked"]]


def assert_coherent(state):
    """Every field must describe the same quarter snapshot."""
    versions = [item["version"] for item in state["detail_versions"]]
    assert versions == list(range(1, len(versions) + 1)), state
    assert state["current_detail_version"] == (versions[-1] if versions else None)
    if state["mask_version"] is None:
        assert state["masked_cells"] == []
        assert "analysis" not in state
    else:
        assert "analysis" in state
        protected = set(state["analysis"]["protected"])
        masked = {(c["row"], c["col"]) for c in state["masked_cells"]}
        # Every protected cell is one of the masked cells; must-public cells
        # are excluded from protection but are not reported separately here.
        assert protected <= {f"{r},{c}" for r, c in masked}, state
    if state["published"]:
        assert state["bulletin_detail_version"] is not None
        assert state["bulletin_detail_version"] <= state["current_detail_version"]
        assert state["published_at"] is not None
    else:
        assert state["bulletin_detail_version"] is None
        assert state["published_at"] is None


def test_status_read_uses_one_snapshot_when_detail_and_publish_land(
        client, monkeypatch):
    """A write committed in the middle of the read stays out of the response."""
    service = client.app.state.service
    register(client)
    assert client.post(f"/api/v1/editor/quarters/{Q}/detail",
                       json={"values": VALUES}).status_code == 200
    cells = advice_cells(client)
    assert client.put(f"/api/v1/editor/quarters/{Q}/mask",
                      json={"cells": [list(c) for c in cells]}
                      ).status_code == 200

    original = service._latest_detail
    fired = {"done": False}

    def interleaved_writer(conn, quarter):
        # Runs after the reader has already read a row: its snapshot is open.
        if not fired["done"]:
            fired["done"] = True
            writer = sqlite3.connect(service.db_path, timeout=30,
                                     isolation_level=None)
            try:
                writer.execute("PRAGMA busy_timeout=30000")
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(
                    "INSERT INTO detail_versions(quarter, version, values_json, "
                    "must_public, created_at, created_by) VALUES(?,?,?,?,?,?)",
                    (Q, 2, json.dumps(ALT), json.dumps([]),
                     "2024-01-02T00:00:00+00:00", None),
                )
                writer.execute(
                    "INSERT INTO mask_plans(quarter, detail_version, "
                    "mask_version, cells_json, updated_at, updated_by) "
                    "VALUES(?,?,?,?,?,?)",
                    (Q, 2, 1, json.dumps([list(c) for c in cells]),
                     "2024-01-02T00:00:00+00:00", None),
                )
                writer.execute(
                    "INSERT INTO bulletins(quarter, detail_version, "
                    "mask_version, payload_json, published_at, published_by) "
                    "VALUES(?,?,?,?,?,?)",
                    (Q, 2, 1, json.dumps({"quarter": Q}),
                     "2024-01-02T00:00:00+00:00", None),
                )
                writer.execute("COMMIT")
            finally:
                writer.close()
        return original(conn, quarter)

    monkeypatch.setattr(service, "_latest_detail", interleaved_writer)

    response = client.get(f"/api/v1/editor/quarters/{Q}")
    assert response.status_code == 200, response.text
    state = response.json()
    assert fired["done"] is True
    assert_coherent(state)

    # The whole response is the state before the concurrent write: no part of
    # version 2 or of its bulletin leaked in.
    assert state["current_detail_version"] == 1
    assert [v["version"] for v in state["detail_versions"]] == [1]
    assert state["mask_version"] == 1
    assert {(c["row"], c["col"]) for c in state["masked_cells"]} == set(cells)
    assert state["published"] is False
    assert state["bulletin_detail_version"] is None

    # A later read sees the complete post-write state instead.
    monkeypatch.setattr(service, "_latest_detail", original)
    after = client.get(f"/api/v1/editor/quarters/{Q}").json()
    assert_coherent(after)
    assert after["current_detail_version"] == 2
    assert [v["version"] for v in after["detail_versions"]] == [1, 2]
    assert after["published"] is True
    assert after["bulletin_detail_version"] == 2


def test_open_status_read_does_not_block_any_writer(client):
    """A held status snapshot must not prevent detail submission or publish."""
    service = client.app.state.service
    register(client)
    register(client, quarter=OTHER)

    reader = connect(service.db_path)
    try:
        reader.execute("BEGIN DEFERRED")
        # First read establishes the snapshot.
        assert reader.execute(
            "SELECT MAX(version) FROM detail_versions WHERE quarter=?", (Q,)
        ).fetchone()[0] is None

        results: dict[str, object] = {}

        def write() -> None:
            results["same_quarter"] = service.submit_detail(
                Q, DetailSubmission(values=VALUES), None)
            results["other_quarter"] = service.submit_detail(
                OTHER, DetailSubmission(values=VALUES), None)

        worker = threading.Thread(target=write, daemon=True)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), "an open status read blocked a writer"
        assert results["same_quarter"]["current_detail_version"] == 1
        assert results["other_quarter"]["current_detail_version"] == 1

        # The reader keeps its earlier snapshot rather than mixing in the new
        # rows it could now see under autocommit.
        assert reader.execute(
            "SELECT MAX(version) FROM detail_versions WHERE quarter=?", (Q,)
        ).fetchone()[0] is None
    finally:
        if reader.in_transaction:
            reader.execute("COMMIT")
        reader.close()

    assert client.get(f"/api/v1/editor/quarters/{Q}").json()[
        "current_detail_version"] == 1


def test_concurrent_status_reads_stay_coherent(client):
    """Hammer the status read while details are resubmitted."""
    service = client.app.state.service
    register(client)
    service.submit_detail(Q, DetailSubmission(values=VALUES), None)

    stop = threading.Event()
    failures: list[tuple[str, dict]] = []

    def read_loop() -> None:
        while not stop.is_set():
            try:
                assert_coherent(service.get_admin_state(Q))
            except AssertionError as exc:  # pragma: no cover - failure path
                failures.append((str(exc), {}))
                stop.set()
                return

    readers = [threading.Thread(target=read_loop, daemon=True) for _ in range(2)]
    for thread in readers:
        thread.start()
    try:
        for index in range(12):
            values = ALT if index % 2 else VALUES
            state = service.submit_detail(Q, DetailSubmission(values=values),
                                          None)
            assert state["current_detail_version"] == index + 2
    finally:
        stop.set()
        for thread in readers:
            thread.join(timeout=10)

    assert not failures


def test_failed_writes_leave_the_status_unchanged(client):
    register(client)
    assert client.post(f"/api/v1/editor/quarters/{Q}/detail",
                       json={"values": VALUES}).status_code == 200
    cells = advice_cells(client)
    assert client.put(f"/api/v1/editor/quarters/{Q}/mask",
                      json={"cells": [list(c) for c in cells]}
                      ).status_code == 200
    before = client.get(f"/api/v1/editor/quarters/{Q}").json()
    assert_coherent(before)

    bad = [row[:] for row in VALUES]
    bad[0][0] += 1
    failed_detail = client.post(f"/api/v1/editor/quarters/{Q}/detail",
                                json={"values": bad})
    assert failed_detail.status_code == 422
    assert client.get(f"/api/v1/editor/quarters/{Q}").json() == before

    failed_publish = client.post(f"/api/v1/editor/quarters/{Q}/publish",
                                 json={"mask_version": 99})
    assert failed_publish.status_code == 409
    assert client.get(f"/api/v1/editor/quarters/{Q}").json() == before
