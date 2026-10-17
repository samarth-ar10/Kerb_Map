import json
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KERB_DB", "local")
    monkeypatch.setenv("KERB_SQLITE", str(tmp_path / "kerb.sqlite"))
    monkeypatch.setenv("KERB_EMBEDDER", "hash")
    monkeypatch.setenv("KERB_LABEL_NOTES", str(tmp_path / "notes.jsonl"))
    jdir = tmp_path / "jsonl"
    jdir.mkdir()
    monkeypatch.setenv("KERB_JSONL_DIRS", str(jdir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from query import api, labels, seed_local, store
    store.reset()
    labels._batches.clear()
    seed_local.main()
    return TestClient(api.app), jdir


def test_health(client):
    c, _ = client
    d = c.get("/api/health").json()
    assert d["ok"] and d["backend"] == "local"
    assert d["db_counts"]["kerb_spaces"] == 5


def test_spaces_radius_and_filters(client):
    c, _ = client
    d = c.get("/api/spaces", params={"lat": 43.68296, "lon": -79.42049, "radius_m": 15}).json()
    assert [s["space_id"] for s in d["spaces"]] == ["TEST_001"]
    assert d["spaces"][0]["is_test"] and "embedding" not in d["spaces"][0]
    d = c.get("/api/spaces", params={"lat": 43.6827, "lon": -79.4200, "radius_m": 500, "state": "free"}).json()
    assert {s["space_id"] for s in d["spaces"]} == {"TEST_001", "TEST_005"}  # TEST_005 placed at its camera
    assert c.get("/api/spaces", params={"state": "parked"}).status_code == 400
    assert c.get("/api/spaces").json()["count"] == 5


def test_receipt(client):
    c, _ = client
    d = c.get("/api/receipt/TEST_002").json()
    assert d["space"]["state"] == "occupied"
    assert [t["step_no"] for t in d["trail"]] == [1, 2, 3]
    assert d["mark"]["box"] == [700, 500, 1100, 800]
    assert d["evidence_uri"].endswith("TEST_frame.svg")
    assert d["evidence_start"] is None and d["evidence_end"] is None  # an image has no clip timing
    assert c.get("/api/receipt/nope").status_code == 404


def test_overrule_before_after(client):
    c, _ = client
    r = c.post("/api/overrule", json={"space_id": "TEST_001", "new_state": "occupied", "by": "tester", "note": "TEST"})
    d = r.json()
    assert d["before"]["state"] == "free" and d["after"]["state"] == "occupied"
    rec = c.get("/api/receipt/TEST_001").json()
    assert rec["space"]["state"] == "occupied" and rec["model_state"] == "free"
    assert rec["trail"][-1]["step"] == "overrule" and len(rec["overrules"]) == 1
    assert c.post("/api/overrule", json={"space_id": "TEST_001", "new_state": "x"}).status_code == 400


def test_evidence(client):
    c, _ = client
    uri = c.get("/api/receipt/TEST_001").json()["evidence_uri"]
    r = c.get("/api/evidence", params={"uri": uri})
    assert r.status_code == 200 and b"TEST FRAME" in r.content
    assert c.get("/api/evidence", params={"uri": "/etc/passwd"}).status_code in (403, 404)


def test_jsonl_fallback(client):
    c, jdir = client
    rows = [{"space_id": "TEST_J1", "source": "dashcam", "side": "L", "state": "free", "lat": 43.6829,
             "lon": -79.4204, "position_status": "ok", "receipt_text": "TEST: jsonl row"},
            {"space_id": "bad", "colour": "red"}]
    (jdir / "kerb_spaces.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (jdir / "trail.jsonl").write_text(json.dumps({"table": "kerb_trail", "trail_id": "TEST_J1_1",
                                                  "space_id": "TEST_J1", "step_no": 1, "step": "gate"}))
    h = c.get("/api/health").json()
    assert h["jsonl_counts"]["kerb_spaces"] == 1 and h["jsonl_skipped_lines"] == 1
    d = c.get("/api/spaces", params={"lat": 43.6829, "lon": -79.4204, "radius_m": 20}).json()
    assert "TEST_J1" in [s["space_id"] for s in d["spaces"]]
    assert len(c.get("/api/receipt/TEST_J1").json()["trail"]) == 1
    # Overruling a JSONL-only space copies it into the database.
    c.post("/api/overrule", json={"space_id": "TEST_J1", "new_state": "occupied"})
    assert c.get("/api/receipt/TEST_J1").json()["space"]["state"] == "occupied"


def test_search_hash_embedder(client):
    c, _ = client
    d = c.get("/api/search", params={"q": "driveway", "lat": 43.6827, "lon": -79.4200, "radius_m": 500}).json()
    assert d["embedder"] == "hash" and "TEST-ONLY" in d["query_explain"]
    assert d["results"][0]["space_id"] == "TEST_003"
    d = c.get("/api/search", params={"q": "driveway", "state": "free"}).json()
    assert {r["space_id"] for r in d["results"]} == {"TEST_001", "TEST_005"}
    rec = c.get("/api/receipt/TEST_003", params={"q": "driveway"}).json()
    assert rec["query_explain"]["embedder"] == "hash"


def test_pages(client):
    c, _ = client
    assert "Kerb Map" in c.get("/").text
    assert "label.js" in c.get("/label").text
    assert "Find parking" in c.get("/").text and "Set up my dashcam" in c.get("/").text
    for f in ("app.js", "api.js", "label.js", "find.js", "style.css", "board.html", "measured.html", "index.html",
              "setup.html", "find.html", "explore.html"):
        assert c.get("/" + f).status_code == 200, f
    assert c.get("/preview-data.json").status_code == 404
    assert c.get("/api/measured").status_code == 404
    assert c.get("/label.html").status_code == 200
    assert c.get("/api.py").status_code == 404
    assert c.get("/health").json() == {"ok": True}


def test_mcp_tools(client):
    from query import mcp_server as m
    out = m.find_free_space(43.6827, -79.4200, 500)
    assert {r["space_id"] for r in out["free"]} == {"TEST_001", "TEST_005"}
    assert out["state_counts"]["cannot_tell"] == 1
    assert m.get_receipt("TEST_004")["space"]["state"] == "cannot_tell"
    assert "error" in m.get_receipt("nope")
    r = m.overrule("TEST_004", "occupied", "TEST note")
    assert r["before"]["state"] == "cannot_tell" and r["after"]["state"] == "occupied"
    assert "error" in m.overrule("TEST_004", "parked", "x")


def test_feedback(client):
    c, _ = client
    r = c.post("/api/feedback", json={"space_id": "TEST_001", "vote": "up"})
    assert r.status_code == 200 and r.json()["new_state"] is None
    r = c.post("/api/feedback", json={"space_id": "TEST_001", "vote": "down", "reason": "taken", "note": "TEST"})
    d = r.json()
    assert d["new_state"] == "occupied" and d["receipt_changed"] is False
    assert c.post("/api/feedback", json={"space_id": "TEST_001", "vote": "down", "reason": "other"}).json()["new_state"] is None
    rec = c.get("/api/receipt/TEST_001").json()
    assert rec["space"]["state"] == "free"  # feedback never changes the receipt
    fb = [o for o in rec["overrules"] if o["by"] == "user-feedback"]
    assert len(fb) == 3 and all(o["receipts_changed"] == [] for o in fb)
    assert {o["new_state"] for o in fb} == {None, "occupied"}
    assert any(o["note"] == "vote=down reason=taken: TEST" for o in fb)
    assert c.post("/api/feedback", json={"space_id": "TEST_001", "vote": "meh"}).status_code == 400
    assert c.post("/api/feedback", json={"space_id": "TEST_001", "vote": "down", "reason": "nope"}).status_code == 400
    assert c.post("/api/feedback", json={"space_id": "TEST_001", "vote": "up", "reason": "taken"}).status_code == 400
    assert c.post("/api/feedback", json={"space_id": "nope", "vote": "up"}).status_code == 404
