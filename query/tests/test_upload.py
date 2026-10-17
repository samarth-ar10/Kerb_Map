"""Upload flow: kerb pipeline on recorded detections, and the job state machine with VSS faked."""

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from common.kerb_schema import to_arrow
from query import kerbpipe

HERE = os.path.dirname(os.path.abspath(__file__))
DET = os.path.join(HERE, "data", "det_set01_0024.json")
SAMPLES = os.path.join(HERE, "..", "samples_gps.json")
JOB = {"job_id": "abc123def0", "camera_id": "dashcam-demo", "filename": "clip.mp4", "object_key": "team-5/clip.mp4",
       "original_video": "s3://b/team-5/clip.mp4"}

# Two parked cars on the right kerb with a gap of ~1.5 car widths between them (fallback calibration a=1.4, y_h=740).
CAR_A = {"label": "car", "confidence": 0.9, "bbox": [1520, 800, 1600, 860]}
CAR_B = {"label": "car", "confidence": 0.9, "bbox": [1760, 820, 1900, 900]}


def synthetic_segments(n=1):
    det = {"fps": 30.0, "frames": [{"frame_index": i, "time_sec": i / 30, "shape": [1080, 1920],
                                    "detections": [CAR_A, CAR_B]} for i in range(150)]}
    return [{"source": f"s3://b/seg_{k + 1}.mp4", "segment_number": k + 1, "start_sec": 5.0 * k, "det": det}
            for k in range(n)]


TRACK = kerbpipe.parse_gps_csv("t_sec,lat,lon,heading_deg,speed_kmh\n0,43.6500,-79.4000,0,36\n5,43.65045,-79.4000,0,36\n")


def recorded():
    with open(DET, encoding="utf-8") as fh:
        segs = json.load(fh)["segments"]
    with open(SAMPLES, encoding="utf-8") as fh:
        track = kerbpipe.track_from_rows(json.load(fh)["samples"]["pie_set01_0024"]["gps"])
    return segs, track


# ---------------------------------------------------------------- pipeline

def test_gps_csv_and_interpolation():
    assert len(TRACK) == 2
    lat, lon, hd = kerbpipe.gps_at(TRACK, 2.5)
    assert abs(lat - 43.650225) < 1e-6 and lon == -79.4 and hd == 0
    assert kerbpipe.gps_at(TRACK, 30) is None


def test_pipeline_on_recorded_detections():
    segs, track = recorded()
    res = kerbpipe.run(segs, track, osm_elements=None, osm_note="no map data")
    slots = res["slots"]
    assert res["stats"]["keyframes"] >= 6 and slots
    assert {s["state"] for s in slots} <= set(kerbpipe.STATES)
    assert any(s["reason"].startswith("lane unclear") and s["state"] == "cannot_tell" for s in slots)
    assert any(s["state"] == "occupied" for s in slots)
    assert all(s["position_status"] == "ok" for s in slots)
    spaces, obs, trail = kerbpipe.build_rows(res, JOB, "2026-10-17T12:00:00Z")
    to_arrow("kerb_spaces", spaces), to_arrow("kerb_observations", obs), to_arrow("kerb_trail", trail)
    assert all(r["space_id"].startswith("up_abc123def0_") for r in spaces)
    assert all(r["camera_id"] == "dashcam-demo" for r in spaces)
    assert len(obs) == len(slots)
    gate = [json.loads(t["detail_json"]) for t in trail if t["step"] == "gate"]
    assert all(g["job_id"] == "abc123def0" and g["filename"] == "clip.mp4" for g in gate)
    # positions near Dundas Street West (PIE GPS of that chunk)
    assert all(abs(r["lat"] - 43.652) < 0.002 and abs(r["lon"] + 79.405) < 0.003 for r in spaces)


def test_no_gps_means_position_uncertain():
    segs, _ = recorded()
    res = kerbpipe.run(segs[:2], None)
    assert res["stats"]["keyframes"] == 10  # 1 per second without GPS
    assert all(s["position_status"] == "uncertain" and s["lat"] is None for s in res["slots"])


def test_gap_free_with_map_and_model_unavailable_without(monkeypatch):
    monkeypatch.delenv("COSMOS3_REASON_URL", raising=False)
    monkeypatch.delenv("KERB_COSMOS_URL", raising=False)
    with_map = kerbpipe.run(synthetic_segments(), TRACK, osm_elements=[], osm_note="empty map")
    gaps = [s for s in with_map["slots"] if s["kind"] == "gap"]
    assert gaps and all(s["state"] == "free" and s["decided_by"] == "rule" for s in gaps)
    no_map = kerbpipe.run(synthetic_segments(), TRACK, osm_elements=None, osm_note="map data unavailable")
    gaps = [s for s in no_map["slots"] if s["kind"] == "gap"]
    assert gaps and all(s["state"] == "cannot_tell" for s in gaps)
    assert all("model unavailable" in s["reason"] or "not sent to model" in s["reason"] for s in gaps)
    assert sum(1 for s in gaps if "model unavailable" in s["reason"]) == kerbpipe.COSMOS_MAX_CALLS


def test_gap_near_hydrant_is_not_space():
    res = kerbpipe.run(synthetic_segments(), TRACK, osm_elements=[], osm_note="x")
    g = [s for s in res["slots"] if s["kind"] == "gap"][0]
    hydrant = [{"type": "node", "id": 7, "lat": g["lat"], "lon": g["lon"], "tags": {"emergency": "fire_hydrant"}}]
    res = kerbpipe.run(synthetic_segments(), TRACK, osm_elements=hydrant, osm_note="x")
    g2 = [s for s in res["slots"] if s["kind"] == "gap"][0]
    assert g2["state"] == "not_space" and "fire hydrant" in g2["reason"]


def test_cosmos_answer_used(monkeypatch):
    monkeypatch.setenv("COSMOS3_REASON_URL", "http://cosmos.invalid")
    monkeypatch.setattr(kerbpipe, "ask_cosmos", lambda video, slot, url, token, model: ("FREE", 0.93, "FREE"))
    res = kerbpipe.run(synthetic_segments(), TRACK, fetch_video=lambda src: b"mp4", osm_elements=None, osm_note="none")
    gaps = [s for s in res["slots"] if s["kind"] == "gap"]
    assert sum(1 for s in gaps if s["state"] == "free" and s["decided_by"] == "cosmos") == kerbpipe.COSMOS_MAX_CALLS
    assert res["stats"]["cosmos_calls"] == kerbpipe.COSMOS_MAX_CALLS


# ---------------------------------------------------------------- job state machine (VSS faked)

@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("KERB_DB", "local")
    monkeypatch.setenv("KERB_SQLITE", str(tmp_path / "kerb.sqlite"))
    monkeypatch.setenv("KERB_JSONL_DIRS", str(tmp_path / "nojsonl"))
    monkeypatch.setenv("KERB_UPLOAD_JOBS", str(tmp_path / "jobs.json"))
    monkeypatch.setenv("KERB_UPLOAD_POLL_S", "0.01")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from query import api, store, upload
    store.reset()
    upload._jobs.clear()
    monkeypatch.setattr(upload.kerbpipe, "fetch_osm", lambda track: (None, "map data switched off"))
    monkeypatch.setattr(upload, "max_upload_mb", lambda: 100.0)
    return TestClient(api.app), upload


def fake_vss(monkeypatch, upload, segs, polls_before_ready=3):
    calls = {"explore": 0, "uploaded": []}

    def vss_upload(name, content):
        calls["uploaded"].append(name)
        return {"success": True, "object_key": "team-5/20261017_120000_" + name, "message": "ok"}

    def explore_items():
        calls["explore"] += 1
        n = calls["explore"]
        if not calls["uploaded"] or n == 1:
            return [{"filename": "20261001_other.mp4", "total_segments": 6, "timeline": [{}] * 6}]
        fn = "20261017_120000_" + calls["uploaded"][-1]
        shown = len(segs) if n > polls_before_ready else min(len(segs), n - 1)
        return [{"filename": fn, "original_video": "s3://b/team-5/" + fn, "total_segments": len(segs),
                 "chunk_duration_sec": 30.0,
                 "timeline": [{"segment_number": s["segment_number"], "segment_start_sec": s["start_sec"],
                               "source": s["source"]} for s in segs[:shown]]}]

    dets = {s["source"]: s["det"] for s in segs}
    monkeypatch.setattr(upload, "vss_upload", vss_upload)
    monkeypatch.setattr(upload, "explore_items", explore_items)
    monkeypatch.setattr(upload, "fetch_detections", lambda src: dets.get(src))
    return calls


def wait_done(c, jid, timeout=20):
    seen = []
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = c.get(f"/api/upload/{jid}").json()
        if not seen or seen[-1] != d["state"]:
            seen.append(d["state"])
        if d["state"] in ("done", "error"):
            return d, seen
        time.sleep(0.01)
    raise AssertionError(f"job stuck: {seen}")


def test_upload_job_with_sample_gps(app_client, monkeypatch):
    c, upload = app_client
    segs, _ = recorded()
    calls = fake_vss(monkeypatch, upload, segs)
    r = c.post("/api/upload", files={"file": ("dundas clip.mp4", b"\x00" * 1000, "video/mp4")},
               data={"sample_id": "pie_set01_0024"})
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    d, seen = wait_done(c, jid)
    assert d["state"] == "done", d
    assert "indexing" in seen or "processing" in seen
    assert [s["name"] for s in d["steps"]] == list(upload.STEPS)
    assert all(s["status"] == "done" and s["at"] for s in d["steps"])
    assert calls["uploaded"] == [f"dundas_clip_{jid}.mp4"]
    assert d["object_key"].endswith(calls["uploaded"][0]) and d["filename"] == "dundas clip.mp4"
    assert d["segments_found"] == 6
    total = sum(d["receipts_by_state"].values())
    assert total > 0 and set(d["receipts_by_state"]) == {"free", "occupied", "not_space", "cannot_tell"}
    assert abs(d["center"]["lat"] - 43.652) < 0.002

    sp = c.get("/api/spaces", params={"job_id": jid}).json()
    assert sp["count"] == total and all(s["job_id"] == jid for s in sp["spaces"])
    near = c.get("/api/spaces", params={"job_id": jid, "lat": d["center"]["lat"], "lon": d["center"]["lon"],
                                        "radius_m": 2000}).json()
    assert near["count"] == total
    assert c.get("/api/spaces", params={"job_id": "ffffffffff"}).json()["count"] == 0
    rec = c.get(f"/api/receipt/{sp['spaces'][0]['space_id']}").json()
    assert [t["step"] for t in rec["trail"]][0] == "gate"

    # after a restart (memory and jobs file gone) the job is rebuilt from its receipts
    upload._jobs.clear()
    os.remove(os.environ["KERB_UPLOAD_JOBS"])
    d2 = c.get(f"/api/upload/{jid}").json()
    assert d2["state"] == "done" and d2["receipts_by_state"] == d["receipts_by_state"]
    assert d2["filename"] == "dundas clip.mp4"


def test_upload_job_gps_csv_overrides(app_client, monkeypatch):
    c, upload = app_client
    segs = synthetic_segments(2)
    fake_vss(monkeypatch, upload, segs, polls_before_ready=1)
    gps = "t_sec,lat,lon,heading_deg,speed_kmh\n0,43.6500,-79.4000,0,36\n10,43.6509,-79.4000,0,36\n"
    jid = c.post("/api/upload", files={"file": ("a.mp4", b"x" * 10, "video/mp4"), "gps": ("g.csv", gps.encode())}).json()["job_id"]
    d, _ = wait_done(c, jid)
    assert d["state"] == "done" and d["gps"].startswith("uploaded CSV")
    assert d["receipts_by_state"]["occupied"] > 0 and abs(d["center"]["lat"] - 43.6504) < 0.001


def test_upload_job_without_gps_is_position_uncertain(app_client, monkeypatch):
    c, upload = app_client
    fake_vss(monkeypatch, upload, synthetic_segments(1), polls_before_ready=1)
    jid = c.post("/api/upload", files={"file": ("b.mp4", b"x", "video/mp4")}).json()["job_id"]
    d, _ = wait_done(c, jid)
    assert d["state"] == "done" and d["center"] is None and "position uncertain" in d["message"]


def test_indexing_timeout_is_error(app_client, monkeypatch):
    c, upload = app_client
    monkeypatch.setenv("KERB_UPLOAD_TIMEOUT_S", "0.05")
    monkeypatch.setattr(upload, "vss_upload", lambda n, b: {"success": True, "object_key": "k/" + n})
    monkeypatch.setattr(upload, "explore_items", lambda: [])
    jid = c.post("/api/upload", files={"file": ("c.mp4", b"x", "video/mp4")}).json()["job_id"]
    d, _ = wait_done(c, jid)
    assert d["state"] == "error" and "did not finish" in d["message"]
    assert d["steps"][0]["status"] == "done" and d["steps"][1]["status"] == "error"
    assert d["steps"][2]["status"] == "pending"


def test_upload_refused_by_vast_is_error(app_client, monkeypatch):
    c, upload = app_client
    monkeypatch.setattr(upload, "vss_upload", lambda n, b: {"success": False, "message": "quota"})
    jid = c.post("/api/upload", files={"file": ("c.mp4", b"x", "video/mp4")}).json()["job_id"]
    d, _ = wait_done(c, jid)
    assert d["state"] == "error" and "quota" in d["message"] and d["steps"][0]["status"] == "error"


def test_upload_bad_requests(app_client):
    c, _ = app_client
    assert c.post("/api/upload", files={"file": ("a.mp4", b"x")}, data={"sample_id": "nope"}).status_code == 400
    assert c.post("/api/upload", data={"sample_id": "pie_set01_0024"}).status_code == 400
    assert c.post("/api/upload", files={"file": ("a.mp4", b"x"), "gps": ("g.csv", b"a,b\n1,2\n")}).status_code == 400
    assert c.get("/api/upload/0000000000").status_code == 404
    s = c.get("/api/upload/samples").json()["samples"]
    assert any(x["sample_id"] == "pie_set01_0024" and x["gps_points"] > 0 for x in s)


def test_ingest_prompt_fits():
    from query import upload
    assert 0 < len(upload.INGEST_PROMPT) <= 800 and "KERB_LEFT" in upload.INGEST_PROMPT


def test_demo_sample_uses_shipped_map(app_client, monkeypatch):
    c, upload = app_client
    s = upload.samples()
    assert {"1", "2", "3"} <= set(s) and all(s[k]["osm"] and len(s[k]["gps"]) >= 20 for k in ("1", "2", "3"))
    segs, _ = recorded()
    fake_vss(monkeypatch, upload, segs, polls_before_ready=1)
    jid = c.post("/api/upload", files={"file": ("kerbmap_sample_2_augusta_ave.mp4", b"x", "video/mp4")}).json()["job_id"]
    d, _ = wait_done(c, jid)
    assert d["state"] == "done" and d["gps"] == "sample 2 (matched by file name)"
    assert "cached for this demo area" in d["steps"][2]["detail"]


def test_sample_without_file_reuses_copy_in_vast_and_latest_done(app_client, monkeypatch):
    c, upload = app_client
    assert c.get("/api/upload/latest-done").status_code == 404
    segs, _ = recorded()
    calls = fake_vss(monkeypatch, upload, segs, polls_before_ready=1)
    # nothing in VAST yet for this sample -> clear error
    jid0 = c.post("/api/upload", files={"sample_id": (None, "2")}).json()["job_id"]
    d0, _ = wait_done(c, jid0)
    assert d0["state"] == "error" and "not in VAST yet" in d0["message"]
    # first a real upload of the sample, then a file-less run that reuses it
    jid1 = c.post("/api/upload", files={"file": ("kerbmap_sample_2_augusta_ave.mp4", b"x", "video/mp4")}).json()["job_id"]
    assert wait_done(c, jid1)[0]["state"] == "done"
    jid2 = c.post("/api/upload", files={"sample_id": (None, "2")}).json()["job_id"]
    d2, _ = wait_done(c, jid2)
    assert d2["state"] == "done" and d2["steps"][0]["detail"].startswith("already in VAST")
    assert len(calls["uploaded"]) == 1 and d2["gps"] == "sample 2"
    assert c.get("/api/upload/latest-done").json()["job_id"] in (jid1, jid2)
    monkeypatch.setenv("KERB_DEMO_JOB", jid1)
    assert c.get("/api/upload/latest-done").json() == {"job_id": jid1}
    assert any(s["sample_id"] == "2" for s in c.get("/api/samples").json()["samples"])


def test_cosmos_failure_stops_further_calls(monkeypatch):
    monkeypatch.setenv("COSMOS3_REASON_URL", "http://cosmos.invalid")
    calls = []

    def boom(*a):
        calls.append(1)
        raise OSError("down")
    monkeypatch.setattr(kerbpipe, "ask_cosmos", boom)
    res = kerbpipe.run(synthetic_segments(), TRACK, fetch_video=lambda src: b"mp4", osm_elements=None, osm_note="none")
    assert len(calls) == 1
    assert all(s["state"] == "cannot_tell" for s in res["slots"] if s["kind"] == "gap")
