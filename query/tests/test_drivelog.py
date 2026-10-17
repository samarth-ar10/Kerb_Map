from fastapi.testclient import TestClient

from query import replay
from query.drivelog import summarize


def test_summarize_counts_only_this_drive():
    videos = ["clip-a.mp4", "clip-b.mp4"]
    spaces = [
        {"original_video": "clip-a.mp4", "state": "free", "street": "Bathurst Street",
         "lat": 43.68, "lon": -79.42, "position_status": "ok", "last_seen": "2026-10-17T12:00:00Z"},
        {"original_video": "clip-a.mp4", "state": "occupied", "street": "Bathurst Street",
         "lat": 43.70, "lon": -79.40, "position_status": "ok", "last_seen": "2026-10-17T12:01:00Z"},
        {"original_video": "clip-b.mp4", "state": "cannot_tell", "street": "",
         "position_status": "uncertain", "receipt_text": "TEST: held out of the pin"},
        {"original_video": "other.mp4", "state": "free", "street": "Queen Street",
         "lat": 1, "lon": 2, "position_status": "ok"},
    ]
    log = summarize("pie_cam-3:set01", videos, spaces)
    assert log["receipts"] == 3 and log["clips"] == 2
    assert log["counts"] == {"free": 1, "occupied": 1, "not_space": 0, "cannot_tell": 1}
    assert log["streets"][0]["street"] == "Bathurst Street" and log["streets"][0]["receipts"] == 2
    assert log["streets"][1]["street"] == "Street unknown"
    assert log["pins"] == 2 and log["position_uncertain"] == 1 and log["test_rows"] == 1
    assert log["lat"] == 43.69 and log["lon"] == -79.41
    assert log["last_seen"] == "2026-10-17T12:01:00Z"
    assert "other.mp4" not in str(log)


def test_drive_log_route(tmp_path, monkeypatch):
    monkeypatch.setenv("KERB_DB", "local")
    monkeypatch.setenv("KERB_SQLITE", str(tmp_path / "kerb.sqlite"))
    monkeypatch.setenv("KERB_JSONL_DIRS", str(tmp_path / "none"))
    geo = tmp_path / "video_geo.csv"
    geo.write_text(
        "camera_id,original_video,chunk_filename\n"
        "pie_cam-3,TEST_v0.mp4,2026_set01_video_chunk_0000.mp4\n"
        "pie_cam-3,TEST_v1.mp4,2026_set01_video_chunk_0001.mp4\n"
    )
    monkeypatch.setenv("KERB_VIDEO_GEO", str(geo))
    monkeypatch.setattr(replay, "HERE", str(tmp_path))
    from query import api, store
    store.reset()
    replay._timeline_cache.update(path=None, mtime=None, drives=None)
    store.db().insert("kerb_spaces", [
        {"space_id": "TEST_log0", "source": "dashcam", "side": "L", "state": "occupied",
         "lat": 43.68, "lon": -79.42, "position_status": "ok", "street": "Vaughan Road",
         "original_video": "TEST_v0.mp4", "receipt_text": "TEST: drive log"},
        {"space_id": "TEST_log1", "source": "dashcam", "side": "R", "state": "free",
         "position_status": "uncertain", "street": "Vaughan Road",
         "original_video": "TEST_v1.mp4", "receipt_text": "TEST: drive log"},
    ])
    client = TestClient(api.app)
    missing = client.get("/api/drives/pie_cam-3:set09")
    assert missing.status_code == 404
    got = client.get("/api/drives/pie_cam-3:set01").json()
    assert got["receipts"] == 2 and got["clips"] == 2
    assert got["counts"]["occupied"] == 1 and got["counts"]["free"] == 1
    assert got["streets"] == [{
        "street": "Vaughan Road", "receipts": 2, "free": 1, "occupied": 1,
        "cannot_tell": 0, "not_space": 0,
    }]
    assert got["pins"] == 1 and got["position_uncertain"] == 1
    assert got["test_rows"] == 2
    page = client.get("/drive.html")
    assert page.status_code == 200 and "api/drives/" in page.text
    assert 'href="/' not in page.text
