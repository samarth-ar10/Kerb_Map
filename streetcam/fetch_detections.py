#!/usr/bin/env python3
"""Save each segment's /videos/detections JSON for one street camera.

Token and backend come from files written by the login step. This script never
prints them. The default camera is neighborhood_cam-1, written under
~/kerb-work/dets/<day> so the existing cache stays put. Other cameras go under
~/kerb-work/dets/<camera_id>/<day>.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_CAMERA = "neighborhood_cam-1"


def work_dir() -> Path:
    return Path(os.environ.get("KERB_WORK") or (Path.home() / "kerb-work"))


def load_auth():
    """Backend URL and token files: KERB_BACKEND_FILE / KERB_TOKEN_FILE, else KERB_WORK/backend, KERB_WORK/token."""
    backend = Path(os.environ.get("KERB_BACKEND_FILE") or (work_dir() / "backend")).read_text().strip().rstrip("/")
    token = Path(os.environ.get("KERB_TOKEN_FILE") or (work_dir() / "token")).read_text().strip()
    return backend, token


def explore_camera(camera: str):
    rows = json.loads(Path(os.environ.get("KERB_EXPLORE") or (work_dir() / "explore.json")).read_text())
    chosen = [r for r in rows if r.get("camera_id") == camera]
    chosen.sort(key=lambda r: r.get("filename") or "")
    if not chosen:
        raise SystemExit(f"no explore rows for {camera}")
    return chosen


def capture_day(camera: str, filename: str) -> str:
    """Day key used to split a camera into tracker runs.

    neighborhood_cam-1 stores the capture date after 'neighborhood_'.
    sf_streets_cam-5 filenames are <uploaddate>_<time>_sf5_chunk_NNNN and have
    no second date, so every chunk shares the date prefix.
    nyc_streets_cam-1 repeats chunk numbers across two VID_YYYYMMDD_HHMMSS clips.
    """
    if camera == "neighborhood_cam-1":
        marker = "neighborhood_"
        i = filename.find(marker)
        if i < 0:
            raise SystemExit(f"cannot read capture day from {filename}")
        return filename[i + len(marker) : i + len(marker) + 8]
    if camera == "sf_streets_cam-5":
        # 20261006_123546_sf5_chunk_0000.mp4
        return filename[:8]
    if camera == "nyc_streets_cam-1":
        marker = "VID_"
        i = filename.find(marker)
        if i < 0:
            raise SystemExit(f"cannot read VID time from {filename}")
        # VID_20261006_171812_002
        return filename[i + 4 : i + 19]
    raise SystemExit(f"no day rule for {camera}")


def build_indexes(camera: str, rows):
    days = {}
    for row in rows:
        day = capture_day(camera, row["filename"])
        days.setdefault(day, []).append(row)
    indexes = {}
    for day, chunks in days.items():
        chunks.sort(key=lambda r: (int(r.get("chunk_index") or 0), r["filename"]))
        out_chunks = []
        for row in chunks:
            timeline = sorted(row.get("timeline") or [], key=lambda s: s.get("segment_number") or 0)
            segs = []
            for seg in timeline:
                n = int(seg["segment_number"])
                segs.append(
                    {
                        "segment_number": n,
                        "start_s": float(seg.get("segment_start_sec") or 0),
                        "end_s": float(seg.get("segment_end_sec") or 0),
                        "source": seg["source"],
                        "file": f"chunk_{int(row['chunk_index']):04d}_seg_{n:03d}.json",
                    }
                )
            duration = float(row.get("chunk_duration_sec") or 0)
            if segs:
                duration = max(duration, max(s["end_s"] for s in segs))
            out_chunks.append(
                {
                    "chunk_index": int(row["chunk_index"]),
                    "filename": row["filename"],
                    "original_video": row.get("original_video"),
                    "duration_s": duration,
                    "location": row.get("location"),
                    "camera_id": row.get("camera_id"),
                    "segments": segs,
                }
            )
        indexes[day] = {
            "camera_id": camera,
            "day": day,
            "n_chunks": len(out_chunks),
            "chunks": out_chunks,
        }
    return indexes


def fetch_one(backend, token, source, dest: Path):
    if dest.exists() and dest.stat().st_size > 0:
        return dest.name, "cached", dest.stat().st_size
    q = urllib.parse.urlencode({"source": source})
    url = f"{backend}/api/v1/videos/detections?{q}"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    last = "error"
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                body = resp.read()
            dest.write_bytes(body)
            return dest.name, "ok", len(body)
        except urllib.error.HTTPError as exc:
            detail = exc.read(120).decode("utf-8", "replace")
            last = f"http_{exc.code}"
            if exc.code not in (429, 500, 502, 503, 504):
                return dest.name, last, detail[:80]
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = type(exc).__name__
        if attempt < 4:
            import time
            time.sleep(2 * (attempt + 1))
    return dest.name, last, "retries_exhausted"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera", default=DEFAULT_CAMERA)
    parser.add_argument("--det-root", type=Path, default=None)
    args = parser.parse_args()
    camera = args.camera
    backend, token = load_auth()
    rows = explore_camera(camera)
    indexes = build_indexes(camera, rows)
    default_root = work_dir() / "dets"
    if camera != DEFAULT_CAMERA:
        default_root = default_root / camera
    root = Path(args.det_root or os.environ.get("DET_ROOT") or default_root)
    jobs = []
    for day, index in indexes.items():
        day_dir = root / day
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n")
        for chunk in index["chunks"]:
            for seg in chunk["segments"]:
                jobs.append((seg["source"], day_dir / seg["file"]))
    ok = cached = failed = 0
    failures = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(fetch_one, backend, token, src, dest) for src, dest in jobs]
        for fut in as_completed(futs):
            name, status, extra = fut.result()
            if status == "ok":
                ok += 1
            elif status == "cached":
                cached += 1
            else:
                failed += 1
                failures.append({"file": name, "status": status, "extra": extra})
    report = {
        "camera_id": camera,
        "days": {d: idx["n_chunks"] for d, idx in indexes.items()},
        "n_segments": len(jobs),
        "downloaded": ok,
        "cached": cached,
        "failed": failed,
        "failures": failures[:20],
    }
    (root / "fetch_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("days", "n_segments", "downloaded", "cached", "failed")}))


if __name__ == "__main__":
    main()
