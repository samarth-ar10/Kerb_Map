#!/usr/bin/env python3
"""Write kerb_spaces, kerb_observations and kerb_trail for the three street cameras.

One kerb_spaces row per legal stretch per 10 s window. Rows are validated through
common.kerbdb. A local SQLite file is always written, and the same rows are
saved as JSONL. VastDB is used as well when the vastdb package imports.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from common.kerb_schema import validate_row
from common import kerbdb

sys.path.insert(0, str(ROOT / "streetcam"))
import geo  # noqa: E402

STILL_S = 120
WINDOW_PX = 25

POSITION_RULE = (
    "A kerb cell (~1 m) is occupied when vehicle box bottoms on this kerb strip cover it "
    "in at least 60% of frames over a 120 s window, whatever the track id; "
    "a parked period lasts at least 120 s."
)
# all three cameras now use the position-based parked.py (kerb cells)
RULES = {cam: POSITION_RULE for cam in ("neighborhood_cam-1", "sf_streets_cam-5", "nyc_streets_cam-1")}
STILL_RULE = (
    f"A car is parked only if its centre stays inside {WINDOW_PX:.0f} px for more than {STILL_S} s "
    "and its box bottom is on this kerb strip."
)

GEO = {
    "neighborhood_cam-1": {
        "lat": None,
        "lon": None,
        "place": "unknown (private neighbourhood camera)",
        "geo_source": "unknown",
        "spec": None,
    },
    "sf_streets_cam-5": {
        "lat": 37.798,
        "lon": -122.40666,
        "place": "Broadway & Columbus Ave, San Francisco (Condor Club, 560 Broadway, in view)",
        "geo_source": "landmark_estimate",
        "spec": ROOT / "streetcam/geo_sf.json",
    },
    "nyc_streets_cam-1": {
        "lat": 40.71878,
        "lon": -74.00255,
        "place": "Broadway & Walker St, Manhattan (401 Broadway and Walker St sign in view)",
        "geo_source": "landmark_estimate",
        "spec": ROOT / "streetcam/geo_nyc.json",
    },
}

# Map positions (streetcam/geo.py, apply_geo.py rules). The projected kerb line is the
# stretch line in geo_<city>.json, not the redrawn occupancy strip in kerb_<city>.json.
# length_m stays the occupancy strip length (the state rule uses it); the projected
# length is recorded as map_length_m in the receipt and the position trail step.
GEO_MAX_USE_M = 15.0  # a fit whose held-out check is over this does not move the row


def fit_geo():
    fits = {}
    for cam, g in GEO.items():
        if g["spec"] and Path(g["spec"]).exists():
            fits[cam] = geo.fit_camera(json.loads(Path(g["spec"]).read_text()))
    return fits


def position_for(cam: str, stretch_id: str, fit):
    """Return (lat, lon, street, position_status, map_length_m, check_m, note)."""
    g = GEO[cam]
    if fit is None:
        return (g["lat"], g["lon"], g["place"], "uncertain", None, None,
                "No map position: video_geo.csv has no lat/lon for this camera.")
    check = fit["check"][0]["error_m"] if fit["check"] else None
    st = (fit["stretches"] or {}).get(stretch_id)
    use = (st is not None and check is not None and check < GEO_MAX_USE_M
           and 1.0 <= st["length_m"] <= 40.0)
    if use:
        status = fit["position_status"]  # ok only when the check is under geo.MAX_ERROR_M (2 m)
        return (st["lat"], st["lon"], st["street"], status, st["length_m"], check,
                f"Map {st['lat']:.6f},{st['lon']:.6f} on {st['street']}, map_length_m {st['length_m']:.3f}. "
                f"position_status {status}. Held-out check {check:.2f} m (ok only under 2 m).")
    return (g["lat"], g["lon"], g["place"], "uncertain", st["length_m"] if st else None, check,
            f"Homography check {check:.2f} m, over 2 m, so this row keeps the video_geo.csv point "
            f"and position_status uncertain." if check is not None else
            "No held-out check point, so position_status uncertain.")


# Paths come from the environment so the script runs on any machine:
#   KERB_WORK          detection work dir (default ~/kerb-work); index under KERB_WORK/dets
#   KERB_DET_ROOT      overrides KERB_WORK/dets
#   KERB_BACKEND_FILE  file holding the backend URL (default KERB_WORK/backend)
#   KERB_TOKEN_FILE    file holding the token (default KERB_WORK/token)
#   RECEIPT_CACHE      scratch dir for downloaded segments and raw frames (default /tmp/receipt-video)
#   STREETCAM_CAMERAS  comma list of camera ids to write (default all three)
WORK = Path(os.environ.get("KERB_WORK") or (Path.home() / "kerb-work"))
DET_ROOT = Path(os.environ.get("KERB_DET_ROOT") or (WORK / "dets"))


def load_auth():
    backend = Path(os.environ.get("KERB_BACKEND_FILE") or (WORK / "backend")).read_text().strip().rstrip("/")
    token = Path(os.environ.get("KERB_TOKEN_FILE") or (WORK / "token")).read_text().strip()
    return backend, token


def clock(day: str, t_sec: float) -> str:
    """Filename date plus seconds along the chunk timeline. Not an index clock."""
    if "_" in day:
        date, hms = day.split("_", 1)
        base = datetime(
            int(date[0:4]), int(date[4:6]), int(date[6:8]),
            int(hms[0:2]), int(hms[2:4]), int(hms[4:6]),
            tzinfo=timezone.utc,
        )
    else:
        base = datetime(int(day[0:4]), int(day[4:6]), int(day[6:8]), tzinfo=timezone.utc)
    return (base + timedelta(seconds=float(t_sec))).strftime("%Y-%m-%dT%H:%M:%SZ")


def locate(index: dict, t: float):
    t0 = 0.0
    chunks = index["chunks"]
    for i, chunk in enumerate(chunks):
        dur = float(chunk["duration_s"])
        last = i == len(chunks) - 1
        if t < t0 + dur or last:
            local = min(max(0.0, t - t0), max(0.0, dur - 1e-3))
            seg = chunk["segments"][-1]
            for cand in chunk["segments"]:
                if float(cand["start_s"]) <= local < float(cand["end_s"]):
                    seg = cand
                    break
            return chunk, seg, local - float(seg["start_s"])
        t0 += dur
    raise RuntimeError("empty index")


def point_in_poly(x, y, poly) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and (yj - yi) != 0:
            xcross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < xcross:
                inside = not inside
        j = i
    return inside


def blur_frame(src: Path, dest: Path, det_path: Path, kerb, time_in_seg: float):
    from PIL import Image, ImageFilter, ImageDraw

    im = Image.open(src).convert("RGB")
    raw = json.loads(det_path.read_text()) if det_path.exists() else {"frames": []}
    frames = raw.get("frames") or []
    frame = min(frames, key=lambda f: abs(float(f.get("time_sec") or 0) - time_in_seg)) if frames else None
    if frame:
        blurred = im.filter(ImageFilter.GaussianBlur(radius=14))
        for det in frame.get("detections") or []:
            box = det.get("bbox")
            if not box or len(box) != 4:
                continue
            x1, y1, x2, y2 = [int(v) for v in box]
            h = max(1, y2 - y1)
            label = det.get("label")
            if label in ("car", "truck", "bus", "motorcycle"):
                top = y1 + int(h * 0.62)
                crop = blurred.crop((x1, top, x2, y2))
                im.paste(crop, (x1, top))
            elif label == "person":
                bot = y1 + max(1, int(h * 0.28))
                crop = blurred.crop((x1, y1, x2, bot))
                im.paste(crop, (x1, y1))
    # outline the kerb strips so the receipt shows which kerb was scored
    dr = ImageDraw.Draw(im)
    for strip in kerb.get("kerb_strips") or []:
        pts = [tuple(p) for p in strip["polygon"]]
        dr.line(pts + [pts[0]], fill=(0, 220, 80) if strip["id"] == "right" or strip["id"] == "far" else (255, 210, 0), width=4)
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.thumbnail((960, 960))
    im.save(dest, quality=60)
    return frame


def download_segment(backend, token, source: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 1000:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = backend + "/api/v1/videos/stream?" + urllib.parse.urlencode({"source": source, "token": token})
    for attempt in range(4):
        proc = subprocess.run(["curl", "-fsS", "-o", str(dest), url], capture_output=True)
        if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 1000:
            return True
        dest.unlink(missing_ok=True)
        import time
        time.sleep(1.5 * (attempt + 1))
    return False


def extract_jpg(mp4: Path, jpg: Path, ss: float) -> bool:
    jpg.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{max(0.0, ss):.2f}", "-i", str(mp4), "-frames:v", "1", "-q:v", "4", str(jpg)],
        capture_output=True,
    )
    return proc.returncode == 0 and jpg.exists()


def state_for(row, stretch, gap_m: float) -> tuple[str, str]:
    """State from the occupied/free metres of this 10 s window.

    occupied when any metres of the stretch are occupied (the reason still gives the
    largest unoccupied gap, and free_m is recorded); free only when nothing is occupied
    and an unoccupied gap of at least gap_m (one car length, 5.5 m) exists; cannot_tell
    when nothing is occupied but the stretch is shorter than one car length.
    """
    free = [(a, b) for a, b in (row.get("free_m") or [])]
    occ = row.get("occupied_m") or []
    ids = row.get("parked_track_ids") or []
    legal = stretch.get("legal") or "hand_marked"
    tail = "legality unknown" if legal == "unknown" else "hand-marked legal stretch"
    widest = max(free, key=lambda g: g[1] - g[0]) if free else None
    gap_txt = (f"widest unoccupied gap {widest[1] - widest[0]:.2f} m at {widest[0]:.2f}-{widest[1]:.2f} m"
               if widest else "no unoccupied gap")
    if occ:
        why = f"occupied metres {occ}; {gap_txt}"
        if ids:
            why += "; parked " + ",".join(ids)
        return "occupied", why + f"; {tail}"
    if widest and widest[1] - widest[0] >= gap_m - 1e-9:
        return "free", f"nothing occupied; {gap_txt} (>= {gap_m} m); {tail}"
    return "cannot_tell", (f"nothing occupied, but the stretch ({float(row['length_m']):.2f} m) is shorter "
                           f"than one car length ({gap_m} m); {tail}")


def main():
    auth = None
    cameras = [
        {
            "camera_id": "neighborhood_cam-1",
            "kerb": ROOT / "streetcam/kerb_neighborhood.json",
            "timeline": ROOT / "streetcam/out/occupancy_timeline.jsonl",
            "index_root": DET_ROOT,
        },
        {
            "camera_id": "sf_streets_cam-5",
            "kerb": ROOT / "streetcam/kerb_sf.json",
            "timeline": ROOT / "streetcam/out/sf/occupancy_timeline.jsonl",
            "index_root": DET_ROOT / "sf_streets_cam-5",
        },
        {
            "camera_id": "nyc_streets_cam-1",
            "kerb": ROOT / "streetcam/kerb_nyc.json",
            "timeline": ROOT / "streetcam/out/nyc/occupancy_timeline.jsonl",
            "index_root": DET_ROOT / "nyc_streets_cam-1",
        },
    ]
    spaces, observations, trails = [], [], []
    counts = {}
    fits = fit_geo()
    cache = Path(os.environ.get("RECEIPT_CACHE") or "/tmp/receipt-video")
    want = [c for c in (os.environ.get("STREETCAM_CAMERAS") or "").split(",") if c]
    if want:
        cameras = [c for c in cameras if c["camera_id"] in want]
    for cam in cameras:
        kerb = json.loads(cam["kerb"].read_text())
        stretches = {s["id"]: s for s in kerb["legal_stretches"]}
        indexes = {}
        rows = [json.loads(line) for line in cam["timeline"].read_text().splitlines() if line.strip()]
        made = {}
        for row in rows:
            day = row["day"]
            if day not in indexes:
                indexes[day] = json.loads((cam["index_root"] / day / "index.json").read_text())
            stretch = stretches[row["stretch_id"]]
            state, reason = state_for(row, stretch, float(kerb.get("free_gap_m", 5.5)))
            chunk, seg, seg_t = locate(indexes[day], float(row["time_s"]))
            key = (cam["camera_id"], day, chunk["chunk_index"])
            rel = Path("streetcam/out/receipts") / cam["camera_id"] / f"{day}_chunk_{int(chunk['chunk_index']):04d}.jpg"
            dest = ROOT / rel
            if key not in made:
                raw_jpg = cache / f"{cam['camera_id']}_{day}_{int(chunk['chunk_index']):04d}.jpg"
                mp4 = cache / f"{cam['camera_id']}_{day}_{int(chunk['chunk_index']):04d}.mp4"
                ok = raw_jpg.exists() and raw_jpg.stat().st_size > 1000
                if not ok:
                    # reuse a frame grabbed earlier when the name matches
                    prior = list(Path("/tmp/nb-frames").glob(f"{day}_{int(chunk['chunk_index']):04d}.jpg")) if cam["camera_id"].startswith("neighborhood") else []
                    prior += list(Path("/tmp/city-frames").glob(f"*{day}*chunk_{int(chunk['chunk_index']):04d}.jpg"))
                    if prior:
                        raw_jpg = prior[0]
                        ok = True
                if not ok:
                    auth = auth or load_auth()
                    backend, token = auth
                    if download_segment(backend, token, seg["source"], mp4):
                        ok = extract_jpg(mp4, raw_jpg, 1.0)
                        mp4.unlink(missing_ok=True)
                det = cam["index_root"] / day / seg["file"]
                if ok:
                    blur_frame(raw_jpg, dest, det, kerb, 1.0)
                    made[key] = rel.as_posix()
                else:
                    made[key] = ""
            evidence = made[key]
            space_id = f"{cam['camera_id']}_{day}_{row['stretch_id']}_t{int(row['time_s'])}"
            lat, lon, street, pos_status, map_len, check_m, pos_note = position_for(
                cam["camera_id"], row["stretch_id"], fits.get(cam["camera_id"]))
            when = clock(day, float(row["time_s"]))
            receipt = (
                f"{cam['camera_id']} {day} {row['stretch_id']} at chunk-timeline {row['time_s']} s "
                f"({chunk['filename']} segment {seg['segment_number']} + {seg_t:.1f} s). "
                f"State {state}: {reason}. Length {row['length_m']} m. "
                f"Occupied metres {row['occupied_m']}. Free metres {row['free_m']}. "
                + RULES.get(cam["camera_id"], STILL_RULE)
                + " " + pos_note
            )
            space = {
                "space_id": space_id,
                "source": "streetcam",
                "camera_id": cam["camera_id"],
                "original_video": chunk.get("original_video") or chunk["filename"],
                "segment_number": int(seg["segment_number"]),
                "t_sec": float(row["time_s"]),
                "side": "view",
                "state": state,
                "reason": reason,
                "confidence": None,
                "lat": lat,
                "lon": lon,
                "position_status": pos_status,
                "street": street,
                "length_m": float(row["length_m"]),
                "last_seen": when,
                "evidence_uri": evidence,
                "receipt_text": receipt,
                "embedding": None,
            }
            obs = {
                "obs_id": space_id + "_obs",
                "space_id": space_id,
                "source": "streetcam",
                "camera_id": cam["camera_id"],
                "segment_source": seg.get("source"),
                "t_sec": float(row["time_s"]),
                "state": state,
                "confidence": None,
            }
            details = [
                ("boxes", {"segment_file": seg["file"], "stretch_id": row["stretch_id"], "occupied_m": row["occupied_m"], "free_m": row["free_m"]}),
                ("track", {"parked_track_ids": row.get("parked_track_ids") or [], **(
                    {"occupancy": kerb.get("occupancy")} if cam["camera_id"] in RULES
                    else {"still_window_px": WINDOW_PX, "still_s": STILL_S})}),
                ("rule", {"text": RULES.get(cam["camera_id"], STILL_RULE) + " Driveway is not_space; lane is traffic. "
                          "Any occupied metres make the window occupied; it is free only if nothing is occupied and a gap of at least free_gap_m exists.", "reason": reason,
                          "free_gap_m": float(kerb.get("free_gap_m", 5.5)), "free_m": row["free_m"]}),
                ("position", {"note": pos_note, "position_status": pos_status, "lat": lat, "lon": lon,
                              "street": street, "length_m": float(row["length_m"]), "map_length_m": map_len,
                              "check_error_m": check_m}),
            ]
            for n, (step, detail) in enumerate(details, start=1):
                trails.append({
                    "trail_id": f"{space_id}_{n}",
                    "space_id": space_id,
                    "step_no": n,
                    "step": step,
                    "detail_json": json.dumps(detail),
                    "at": when,
                })
            spaces.append(validate_row("kerb_spaces", space))
            observations.append(validate_row("kerb_observations", obs))
            bucket = counts.setdefault(cam["camera_id"], {"spaces": 0, "free": 0, "occupied": 0, "frames": 0})
            bucket["spaces"] += 1
            bucket[state] = bucket.get(state, 0) + 1
            bucket["position_" + pos_status] = bucket.get("position_" + pos_status, 0) + 1
        counts[cam["camera_id"]]["frames"] = len({v for v in made.values() if v})

    cam_rows = []
    for cam in cameras:
        cid = cam["camera_id"]
        g, fit = GEO[cid], fits.get(cid)
        cam_rows.append(validate_row("kerb_cameras", {
            "camera_id": cid, "kind": "street_camera", "lat": g["lat"], "lon": g["lon"], "heading": None,
            "place": g["place"], "geo_source": g["geo_source"], "geo_confidence": None,
            "check_error_m": (fit["check"][0]["error_m"] if fit and fit["check"] else None),
        }))

    out = ROOT / "streetcam/out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "geo_fit.json").write_text(json.dumps(fits, indent=2))
    for name, rows in (("kerb_spaces", spaces), ("kerb_observations", observations), ("kerb_trail", trails),
                       ("kerb_cameras", cam_rows)):
        path = out / f"{name}.jsonl"
        with path.open("w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        print(name, len(rows), path)

    want_vastdb = os.environ.get("KERB_DB") == "vastdb"
    os.environ.setdefault("KERB_SQLITE", str(Path.home() / "kerb-data" / "kerb.sqlite"))
    db = kerbdb.get_db("local")
    db.ensure_tables()
    # replace previous streetcam rows by writing a fresh sqlite if this is the streetcam copy
    db.insert("kerb_spaces", spaces)
    db.insert("kerb_observations", observations)
    db.insert("kerb_trail", trails)
    db.insert("kerb_cameras", cam_rows)
    print("sqlite", os.environ["KERB_SQLITE"], "spaces", len(db.read("kerb_spaces", {"source": "streetcam"}, limit=100000)))

    vastdb_status = "not_attempted (set KERB_DB=vastdb to write VastDB)"
    if want_vastdb:
        try:
            import vastdb  # noqa: F401
            vdb = kerbdb.get_db("vastdb")
            vdb.ensure_tables()
            vdb.insert("kerb_spaces", spaces)
            vdb.insert("kerb_observations", observations)
            vdb.insert("kerb_trail", trails)
            vdb.insert("kerb_cameras", cam_rows)
            vastdb_status = "ok"
        except Exception as exc:
            vastdb_status = type(exc).__name__ + ": " + str(exc).split("\n")[0][:180]
    print("vastdb", vastdb_status)
    print("counts", json.dumps(counts))


if __name__ == "__main__":
    main()
