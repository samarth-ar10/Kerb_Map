#!/usr/bin/env python3
"""Link YOLO boxes for one neighborhood camera day into tracks.

Reads the detection JSON written by fetch_detections.py. Does not call the
network and does not re-run YOLO. Association is greedy IoU against a
constant-velocity prediction, in the spirit of ByteTrack's first association
stage. Tracks stay open across segments and chunks of the same day.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


HIGH_IOU = 0.30
MAX_GAP_S = 2.0
STILL_SPEED_PX_S = 20.0
STILL_MAX_DT_S = 0.5


def box_iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def center(box):
    return ((box[0] + box[2]) * 0.5, (box[1] + box[3]) * 0.5)


def predict_box(track, t):
    dt = max(0.0, t - track["last_t"])
    cx, cy = center(track["last_box"])
    w = track["last_box"][2] - track["last_box"][0]
    h = track["last_box"][3] - track["last_box"][1]
    pcx = cx + track["vx"] * dt
    pcy = cy + track["vy"] * dt
    return [pcx - w * 0.5, pcy - h * 0.5, pcx + w * 0.5, pcy + h * 0.5]


def load_index(path: Path) -> dict:
    return json.loads(path.read_text())


def iter_frames(index: dict, det_dir: Path):
    """Yield observations in time order for one day."""
    chunk_t0 = 0.0
    for chunk in index["chunks"]:
        duration = float(chunk["duration_s"])
        for seg in chunk["segments"]:
            raw = json.loads((det_dir / seg["file"]).read_text())
            fps = float(raw.get("fps") or 0)
            seg_start = float(seg["start_s"])
            for frame in raw.get("frames") or []:
                t = chunk_t0 + seg_start + float(frame.get("time_sec") or 0)
                dets = []
                for det in frame.get("detections") or []:
                    box = det.get("bbox")
                    if not box or len(box) != 4:
                        continue
                    dets.append(
                        {
                            "label": det.get("label") or "object",
                            "confidence": float(det.get("confidence") or 0),
                            "box": [float(v) for v in box],
                        }
                    )
                yield {
                    "t": t,
                    "fps": fps,
                    "chunk_index": chunk["chunk_index"],
                    "filename": chunk["filename"],
                    "segment_number": seg["segment_number"],
                    "frame_index": int(frame.get("frame_index") or 0),
                    "shape": frame.get("shape"),
                    "detections": dets,
                }
        chunk_t0 += duration


def associate(frames):
    active = []
    finished = []
    next_id = 1

    def retire(track):
        finished.append(track)

    for fr in frames:
        t = fr["t"]
        still_alive = []
        for track in active:
            if t - track["last_t"] <= MAX_GAP_S:
                still_alive.append(track)
            else:
                retire(track)
        active = still_alive

        dets = fr["detections"]
        pairs = []
        preds = [predict_box(tr, t) for tr in active]
        for i, tr in enumerate(active):
            for j, det in enumerate(dets):
                if det["label"] != tr["label"]:
                    continue
                ov = box_iou(preds[i], det["box"])
                if ov >= HIGH_IOU:
                    pairs.append((ov, i, j))
        pairs.sort(reverse=True)
        used_t, used_d = set(), set()
        matches = []
        for ov, i, j in pairs:
            if i in used_t or j in used_d:
                continue
            used_t.add(i)
            used_d.add(j)
            matches.append((i, j))

        for i, j in matches:
            tr = active[i]
            det = dets[j]
            dt = t - tr["last_t"]
            ocx, ocy = center(tr["last_box"])
            ncx, ncy = center(det["box"])
            if 0 < dt <= MAX_GAP_S:
                meas_vx = (ncx - ocx) / dt
                meas_vy = (ncy - ocy) / dt
                tr["vx"] = 0.6 * meas_vx + 0.4 * tr["vx"]
                tr["vy"] = 0.6 * meas_vy + 0.4 * tr["vy"]
            tr["last_t"] = t
            tr["last_box"] = det["box"]
            tr["obs"].append(_obs(fr, det))

        for j, det in enumerate(dets):
            if j in used_d:
                continue
            active.append(
                {
                    "local_id": next_id,
                    "label": det["label"],
                    "vx": 0.0,
                    "vy": 0.0,
                    "last_t": t,
                    "last_box": det["box"],
                    "obs": [_obs(fr, det)],
                }
            )
            next_id += 1

    for tr in active:
        retire(tr)
    return finished


def _obs(fr, det):
    return {
        "t": fr["t"],
        "chunk_index": fr["chunk_index"],
        "filename": fr["filename"],
        "segment_number": fr["segment_number"],
        "frame_index": fr["frame_index"],
        "box": det["box"],
        "confidence": det["confidence"],
    }


def still_seconds(obs) -> float:
    total = 0.0
    for a, b in zip(obs, obs[1:]):
        dt = b["t"] - a["t"]
        if dt <= 0 or dt > STILL_MAX_DT_S:
            continue
        ax, ay = center(a["box"])
        bx, by = center(b["box"])
        speed = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5 / dt
        if speed < STILL_SPEED_PX_S:
            total += dt
    return total


def mean_box(obs):
    n = len(obs)
    acc = [0.0, 0.0, 0.0, 0.0]
    for o in obs:
        for k in range(4):
            acc[k] += o["box"][k]
    return [acc[k] / n for k in range(4)]


def to_record(day: str, track: dict) -> dict:
    obs = track["obs"]
    return {
        "track_id": f"{day}-{track['local_id']:04d}",
        "day": day,
        "label": track["label"],
        "frames": [
            {
                "chunk_index": o["chunk_index"],
                "filename": o["filename"],
                "segment_number": o["segment_number"],
                "frame_index": o["frame_index"],
                "time_s": o["t"],
            }
            for o in obs
        ],
        "start_time_s": obs[0]["t"],
        "end_time_s": obs[-1]["t"],
        "box_path": [o["box"] for o in obs],
        "mean_box": mean_box(obs),
        "still_seconds": still_seconds(obs),
        "n_obs": len(obs),
    }


def chunk_coverage(index: dict, records: list[dict]) -> list[dict]:
    by_chunk = {c["chunk_index"]: [] for c in index["chunks"]}
    for rec in records:
        seen = {fr["chunk_index"] for fr in rec["frames"]}
        for ci in seen:
            if ci in by_chunk:
                by_chunk[ci].append(rec["track_id"])
    rows = []
    for chunk in index["chunks"]:
        ids = by_chunk.get(chunk["chunk_index"], [])
        rows.append(
            {
                "chunk_index": chunk["chunk_index"],
                "filename": chunk["filename"],
                "n_segments": len(chunk["segments"]),
                "n_tracks": len(ids),
                "track_ids": ids,
            }
        )
    return rows


def boundary_report(index: dict, det_dir: Path) -> list[dict]:
    """Best IoU between the last frame of chunk n and the first of chunk n+1."""
    chunks = index["chunks"]
    report = []
    for a, b in zip(chunks, chunks[1:]):
        if not a["segments"] or not b["segments"]:
            report.append({"from": a["chunk_index"], "to": b["chunk_index"], "best_iou": None})
            continue
        ja = json.loads((det_dir / a["segments"][-1]["file"]).read_text())
        jb = json.loads((det_dir / b["segments"][0]["file"]).read_text())
        fa = (ja.get("frames") or [None])[-1]
        fb = (jb.get("frames") or [None])[0]
        boxes_a = [d["bbox"] for d in (fa or {}).get("detections") or [] if d.get("bbox")]
        boxes_b = [d["bbox"] for d in (fb or {}).get("detections") or [] if d.get("bbox")]
        best = 0.0
        for ba in boxes_a:
            for bb in boxes_b:
                best = max(best, box_iou(ba, bb))
        report.append(
            {
                "from": a["chunk_index"],
                "to": b["chunk_index"],
                "from_file": a["filename"],
                "to_file": b["filename"],
                "n_end": len(boxes_a),
                "n_start": len(boxes_b),
                "best_iou": best,
            }
        )
    return report


def run(day: str, det_dir: Path, out_dir: Path) -> dict:
    index = load_index(det_dir / "index.json")
    if index.get("day") != day:
        raise SystemExit(f"index day {index.get('day')} != {day}")
    frames = list(iter_frames(index, det_dir))
    tracks = associate(frames)
    records = [to_record(day, tr) for tr in tracks if tr["obs"]]
    records.sort(key=lambda r: (r["start_time_s"], r["track_id"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "tracks.jsonl"
    with out_path.open("w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    coverage = chunk_coverage(index, records)
    summary = {
        "day": day,
        "n_chunks": len(index["chunks"]),
        "n_chunk_files_in_index": len(index["chunks"]),
        "n_frames": len(frames),
        "n_tracks": len(records),
        "chunks_with_tracks": sum(1 for c in coverage if c["n_tracks"] > 0),
        "chunks_without_tracks": sum(1 for c in coverage if c["n_tracks"] == 0),
        "coverage": coverage,
        "boundaries": boundary_report(index, det_dir),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--day", required=True)
    p.add_argument("--detections", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    summary = run(args.day, args.detections, args.out)
    print(
        json.dumps(
            {
                "day": summary["day"],
                "n_chunks": summary["n_chunks"],
                "n_frames": summary["n_frames"],
                "n_tracks": summary["n_tracks"],
                "chunks_without_tracks": summary["chunks_without_tracks"],
            }
        )
    )


if __name__ == "__main__":
    main()
