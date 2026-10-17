import pytest
from fastapi.testclient import TestClient

from query import labels


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KERB_DB", "local")
    monkeypatch.setenv("KERB_SQLITE", str(tmp_path / "kerb.sqlite"))
    monkeypatch.setenv("KERB_EMBEDDER", "hash")
    monkeypatch.setenv("KERB_LABEL_NOTES", str(tmp_path / "notes.jsonl"))
    monkeypatch.setenv("KERB_JSONL_DIRS", str(tmp_path / "none"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from query import api, store
    store.reset()
    labels._batches.clear()
    db = store.db()
    rows = []
    for i in range(60):
        state = ["free", "occupied", "not_space", "cannot_tell"][i % 4]
        rows.append({"space_id": f"TEST_{i:03d}", "source": "dashcam" if i % 2 else "streetcam",
                     "side": "L", "state": state, "confidence": 0.9 if i % 3 else 0.3,
                     "lat": 43.68 + i * 1e-4, "lon": -79.42, "position_status": "ok",
                     "receipt_text": "TEST: batch row", "evidence_uri": "TEST_frame.svg"})
    db.insert("kerb_spaces", rows)
    return TestClient(api.app)


def dev_ids():
    return [f"TEST_{i:03d}" for i in range(60) if labels.split_of(f"TEST_{i:03d}") == "dev"]


def test_claim_is_exclusive_and_prioritised(client):
    a = client.post("/api/batches/claim", json={"labeller": "ann"}).json()
    b = client.post("/api/batches/claim", json={"labeller": "bob"}).json()
    ia, ib = [i["item_id"] for i in a["items"]], [i["item_id"] for i in b["items"]]
    assert len(ia) == 20 and all(labels.split_of(i) == "dev" for i in ia)
    shared = set(ia) & set(ib)
    assert all(labels.is_overlap(i) for i in shared)  # only overlap items go to two people
    # low confidence / cannot_tell come first
    first = a["items"][0]["space"]
    assert first["state"] == "cannot_tell" or first["confidence"] < 0.6
    # claiming again returns the same open batch
    assert client.post("/api/batches/claim", json={"labeller": "ann"}).json()["batch_id"] == a["batch_id"]


def test_heldout_pool_separate(client):
    h = client.post("/api/batches/claim", json={"labeller": "ann", "split": "heldout"}).json()
    assert h["items"] and all(i["split"] == "heldout" for i in h["items"])


def test_label_supersede_board_summary_export(client):
    a = client.post("/api/batches/claim", json={"labeller": "ann"}).json()
    item = a["items"][0]
    iid = item["item_id"]
    r = client.post("/api/labels", json={"item_id": iid, "labeller": "ann", "label": "free", "ms_taken": 1200}).json()
    assert r["split"] == "dev"
    r2 = client.post("/api/labels", json={"item_id": iid, "labeller": "ann", "label": "not_space",
                                          "reason": "driveway", "note": "TEST"}).json()
    latest = labels.latest_labels()[iid]
    assert list(latest) == ["ann"] and latest["ann"]["label"] == "not_space/driveway"
    assert r2["agrees"] == (item["model_state"] == "not_space")
    b = client.get("/api/board").json()
    ann = {x["labeller"]: x for x in b["labellers"]}["ann"]
    assert ann["done"] == 1 and ann["in_progress"] == 19
    assert b["progress"]["dev"] == {"done": 1, "total": len(dev_ids())}
    s = client.get("/api/labels/summary").json()
    assert s["items_compared"] == 1 and s["total_labels"] == 1
    assert s["by_labeller"]["ann"] == {"labels": 1, "compared": 1, "agree": int(item["model_state"] == "not_space")}
    assert a["reasons"] == list(labels.NOT_SPACE_REASONS)
    assert item["space_id"] == iid and "clip_start" in item and "evidence_uri" in item
    if item["model_state"] != "not_space":
        assert s["top_failures"][0] == {"what": f"not_space/driveway called {item['model_state']}", "count": 1}
        assert s["disagreements"][0]["item_id"] == iid
    e = client.get("/api/labels/export", params={"split": "dev"}).json()
    assert e["count"] == 1 and len(e["frozen_sha256"]) == 64
    assert client.get("/api/labels/export", params={"split": "dev"}).json()["frozen_sha256"] == e["frozen_sha256"]
    csv = client.get("/api/labels/export", params={"split": "dev", "format": "csv"})
    assert csv.headers["x-export-sha256"] == e["frozen_sha256"] and "not_space/driveway" in csv.text
    assert client.get("/api/labels/export", params={"split": "heldout"}).json()["count"] == 0


def test_disagreement_needs_third_look(client):
    iid = dev_ids()[0]
    client.post("/api/labels", json={"item_id": iid, "labeller": "ann", "label": "free"})
    client.post("/api/labels", json={"item_id": iid, "labeller": "bob", "label": "occupied"})
    b = client.get("/api/board").json()
    assert b["needs_third_look"] == 1 and [t["item_id"] for t in b["needs_third_look_items"]] == [iid]
    assert {x["labeller"]: x for x in b["labellers"]}["ann"]["agreement"] == 0.0
    t = client.post("/api/batches/claim", json={"labeller": "dan", "kind": "third_look"}).json()
    assert [i["item_id"] for i in t["items"]] == [iid]
    # dan now holds the third look, so nobody else gets it; once released it is served first
    c = client.post("/api/batches/claim", json={"labeller": "cat"}).json()
    assert iid not in [i["item_id"] for i in c["items"]]
    client.post(f"/api/batches/{t['batch_id']}/release")
    client.post(f"/api/batches/{c['batch_id']}/release")
    e = client.post("/api/batches/claim", json={"labeller": "eve"}).json()
    assert e["items"][0]["item_id"] == iid
    s = client.get("/api/labels/summary").json()
    assert s["no_consensus"] == 1


def test_release_frees_items(client):
    a = client.post("/api/batches/claim", json={"labeller": "ann"}).json()
    client.post(f"/api/batches/{a['batch_id']}/release")
    b = client.post("/api/batches/claim", json={"labeller": "bob"}).json()
    assert {i["item_id"] for i in a["items"]} == {i["item_id"] for i in b["items"]}
    assert client.post("/api/batches/nope/release").status_code == 404


def test_lock_expires(client, monkeypatch):
    a = client.post("/api/batches/claim", json={"labeller": "ann"}).json()
    for b in labels._batches.values():
        b["expires"] = 0
    b = client.post("/api/batches/claim", json={"labeller": "bob"}).json()
    assert {i["item_id"] for i in a["items"]} == {i["item_id"] for i in b["items"]}


def test_label_validation(client):
    iid = dev_ids()[0]
    assert client.post("/api/labels", json={"item_id": iid, "labeller": " ", "label": "free"}).status_code == 400
    assert client.post("/api/labels", json={"item_id": iid, "labeller": "a", "label": "parked"}).status_code == 400
    assert client.post("/api/labels", json={"item_id": "nope", "labeller": "a", "label": "free"}).status_code == 404
    assert client.post("/api/labels", json={"item_id": iid, "labeller": "a", "label": "not_space",
                                            "reason": "vibes"}).status_code == 400
    for r in labels.NOT_SPACE_REASONS:
        assert client.post("/api/labels", json={"item_id": iid, "labeller": "a", "label": "not_space",
                                                "reason": r}).status_code == 200
