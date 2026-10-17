import pytest
from fastapi.testclient import TestClient

from query import replay


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KERB_DB", "local")
    monkeypatch.setenv("KERB_SQLITE", str(tmp_path / "kerb.sqlite"))
    monkeypatch.setenv("KERB_JSONL_DIRS", str(tmp_path / "none"))
    geo = tmp_path / "video_geo.csv"
    lines = ["camera_id,original_video,chunk_filename,segment_number"]
    for i in range(3):
        for seg in (1, 2):
            lines.append(f"pie_cam-3,TEST_v{i}.mp4,2026_set01_video_chunk_000{i}.mp4,{seg}")
    lines.append("neighborhood_cam-1,TEST_n0.mp4,2026_neighborhood_chunk_0000.mp4,1")
    geo.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("KERB_VIDEO_GEO", str(geo))
    monkeypatch.setattr(replay, "HERE", str(tmp_path))  # ignore any built replay_chunks.csv
    from query import api, store
    store.reset()
    replay._state["run"] = None
    replay._timeline_cache.update(path=None, mtime=None, drives=None)
    store.db().insert("kerb_spaces", [
        {"space_id": f"TEST_r{i}", "source": "dashcam", "side": "L", "state": "free", "lat": 43.68,
         "lon": -79.42, "position_status": "ok", "original_video": f"TEST_v{i}.mp4", "receipt_text": "TEST: replay"}
        for i in range(3)])
    return TestClient(api.app)


def test_drives(client):
    d = client.get("/api/replay/drives").json()
    assert d["mode"] == "replay"
    got = {x["drive_id"]: x for x in d["drives"]}
    assert got["pie_cam-3:set01"]["clips"] == 3 and got["pie_cam-3:set01"]["receipts"] == 3
    assert "neighborhood_cam-1" in got


def test_replay_releases_chunks_over_time(client):
    st0 = client.get("/api/replay/status").json()
    assert st0["active"] is False and st0["spaces"] == [] and st0["mode"] == "replay"
    s = client.post("/api/replay/start", json={"drive_id": "pie_cam-3:set01", "speed": 4}).json()
    assert s["mode"] == "replay" and s["clips_total"] == 3 and s["clips_done"] == 0 and s["replay_id"]
    # pretend 20 s of wall time passed at 4x = 80 s of drive: two chunks have arrived
    replay._state["run"]["started"] -= 20
    st = client.get("/api/replay/status", params={"replay_id": s["replay_id"]}).json()
    assert st["clips_done"] == 2 and st["current_clip"] == "TEST_v1.mp4" and st["active"]
    assert [r["space_id"] for r in st["spaces"]] == ["TEST_r0", "TEST_r1"]
    assert client.get("/api/replay/status", params={"replay_id": "other"}).json()["active"] is False
    sp = client.get("/api/spaces", params={"as_of": st["as_of"]}).json()
    assert sp["mode"] == "replay" and sorted(r["space_id"] for r in sp["spaces"]) == ["TEST_r0", "TEST_r1"]
    assert client.get("/api/spaces", params={"as_of": 10}).json()["count"] == 0
    assert client.get("/api/spaces").json()["count"] == 3  # without as_of nothing is hidden
    replay._state["run"]["started"] -= 100
    assert client.get("/api/replay/status").json()["active"] is False
    assert len(client.get("/api/replay/status").json()["spaces"]) == 3


def test_bad_start(client):
    assert client.post("/api/replay/start", json={"set": "set09"}).status_code == 404
    assert client.post("/api/replay/start", json={"camera_id": "neighborhood_cam-1", "speed": 0}).status_code == 400
    assert client.post("/api/replay/start", json={"camera_id": "neighborhood_cam-1"}).json()["clips_total"] == 1
    assert client.post("/api/replay/stop", json={"replay_id": "x"}).json()["running"] is False
