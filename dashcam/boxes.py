"""Read stored YOLO boxes for one segment. Does not re-run the detector."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from urllib.parse import quote

from keyframes import keyframe_times
from session import get_json, login

VEHICLES = frozenset({"car", "truck", "bus"})


def vehicle_boxes(payload: dict) -> list[dict]:
    frames = []
    for frame in payload.get("frames") or []:
        boxes = []
        for box in frame.get("detections") or []:
            label = box.get("label")
            if label not in VEHICLES:
                continue
            boxes.append(
                {
                    "label": label,
                    "confidence": box.get("confidence"),
                    "bbox": box.get("bbox"),
                }
            )
        frames.append(
            {
                "frame_index": frame.get("frame_index"),
                "time_sec": frame.get("time_sec"),
                "boxes": boxes,
            }
        )
    return frames


def fetch_segment(backend: str, token: str, source: str) -> dict:
    payload = get_json(
        backend,
        token,
        "/api/v1/videos/detections?source=" + quote(source, safe=""),
    )
    shape = payload.get("video_shape") or [1080, 1920]
    return {
        "source": payload.get("segment_source") or source,
        "fps": payload.get("fps"),
        "frame_count": payload.get("frame_count"),
        "height": int(shape[0]),
        "width": int(shape[1]),
        "frames": vehicle_boxes(payload),
    }


def frame_at(frames: list[dict], time_sec: float) -> dict:
    if not frames:
        raise RuntimeError("detection payload has no frames")
    chosen = min(frames, key=lambda frame: abs(float(frame["time_sec"]) - time_sec))
    delta = abs(float(chosen["time_sec"]) - time_sec)
    if delta > 0.5:
        raise RuntimeError(f"no detection frame within 0.5 s of {time_sec}")
    return chosen


def check_keyframes(segments_csv: Path, report: Path, map_path: Path) -> dict:
    backend, token = login()
    cache: dict[str, dict] = {}
    report.parent.mkdir(parents=True, exist_ok=True)
    ok = 0
    failed = 0
    with segments_csv.open(newline="") as handle, report.open("w", newline="") as out:
        reader = csv.DictReader(handle)
        writer = csv.DictWriter(
            out,
            fieldnames=[
                "set",
                "chunk",
                "segment_number",
                "time_in_segment_sec",
                "frame_index",
                "n_car",
                "n_truck",
                "n_bus",
                "ok",
                "error",
            ],
        )
        writer.writeheader()
        for row in reader:
            source = row["segment_source_uri"]
            duration = float(row["end_sec_in_set"]) - float(row["start_sec_in_set"])
            times = keyframe_times(duration, row["set"], int(row["chunk"]), map_path)
            try:
                if source not in cache:
                    cache[source] = fetch_segment(backend, token, source)
                frames = cache[source]["frames"]
            except Exception as exc:  # noqa: BLE001 — record the failure per segment
                for time_sec in times:
                    failed += 1
                    writer.writerow(
                        {
                            "set": row["set"],
                            "chunk": row["chunk"],
                            "segment_number": row["segment_number"],
                            "time_in_segment_sec": time_sec,
                            "frame_index": "",
                            "n_car": "",
                            "n_truck": "",
                            "n_bus": "",
                            "ok": "no",
                            "error": str(exc),
                        }
                    )
                continue
            for time_sec in times:
                try:
                    frame = frame_at(frames, time_sec)
                    counts = {"car": 0, "truck": 0, "bus": 0}
                    for box in frame["boxes"]:
                        counts[box["label"]] = counts.get(box["label"], 0) + 1
                    ok += 1
                    writer.writerow(
                        {
                            "set": row["set"],
                            "chunk": row["chunk"],
                            "segment_number": row["segment_number"],
                            "time_in_segment_sec": time_sec,
                            "frame_index": frame["frame_index"],
                            "n_car": counts["car"],
                            "n_truck": counts["truck"],
                            "n_bus": counts["bus"],
                            "ok": "yes",
                            "error": "",
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    writer.writerow(
                        {
                            "set": row["set"],
                            "chunk": row["chunk"],
                            "segment_number": row["segment_number"],
                            "time_in_segment_sec": time_sec,
                            "frame_index": "",
                            "n_car": "",
                            "n_truck": "",
                            "n_bus": "",
                            "ok": "no",
                            "error": str(exc),
                        }
                    )
    return {"ok": ok, "failed": failed, "segments": len(cache) + failed}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch car/truck/bus boxes for a segment")
    parser.add_argument("source", nargs="?", help="Segment s3 URI")
    parser.add_argument("--keyframes", type=Path, help="Segment CSV to check at each keyframe")
    parser.add_argument("--report", type=Path, help="Where to write the keyframe box report")
    parser.add_argument(
        "--pie-map",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "pie_map.csv",
    )
    args = parser.parse_args()
    if args.keyframes:
        if not args.report:
            raise SystemExit("--report is required with --keyframes")
        summary = check_keyframes(args.keyframes, args.report, args.pie_map)
        print(json.dumps(summary))
        return
    if not args.source:
        raise SystemExit("pass a segment source URI, or --keyframes")
    backend, token = login()
    payload = fetch_segment(backend, token, args.source)
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
