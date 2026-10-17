#!/usr/bin/env python3
"""List neighborhood_cam-1 from the retrieval explore API and save each
segment's /videos/detections JSON. Token and backend come from files written
by the login step. This script never prints them.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CAMERA = "neighborhood_cam-1"


def load_auth():
    home = Path.home() / "kerb-work"
    backend = (home / "backend").read_text().strip().rstrip("/")
    token = (home / "token").read_text().strip()
    return backend, token


def explore_neighborhood():
    rows = json.loads((Path.home() / "kerb-work" / "explore.json").read_text())
    nb = [r for r in rows if r.get("camera_id") == CAMERA]
    def sort_key(r):
        name = r.get("filename") or ""
        # capture day is the token before _chunk_, upload stamp is the prefix
        return name
    nb.sort(key=sort_key)
    return nb


def capture_day(filename: str) -> str:
    # 20261001_072331_neighborhood_20260901_chunk_0000.mp4
    marker = "neighborhood_"
    i = filename.find(marker)
    if i < 0:
        raise SystemExit(f"cannot read capture day from {filename}")
    return filename[i + len(marker) : i + len(marker) + 8]


def build_indexes(rows):
    days = {}
    for row in rows:
        day = capture_day(row["filename"])
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
            "camera_id": CAMERA,
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
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(200)
        return dest.name, f"http_{exc.code}", detail.decode("utf-8", "replace")[:80]
    dest.write_bytes(body)
    return dest.name, "ok", len(body)


def main():
    backend, token = load_auth()
    rows = explore_neighborhood()
    indexes = build_indexes(rows)
    root = Path(os.environ.get("DET_ROOT", str(Path.home() / "kerb-work" / "dets")))
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
    with ThreadPoolExecutor(max_workers=8) as pool:
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
        "camera_id": CAMERA,
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
