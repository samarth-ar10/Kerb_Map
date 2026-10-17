"""Build left/right kerb slots from stored boxes on each keyframe.

Image geometry only. One car length is the mean width of the two cars that
bound a gap, not a metre measurement. A shorter gap is not a space. A gap
longer than two and a half of those widths is not called one space: it is the
driveway or empty-kerb case. A side with no parked-looking row produces no gap.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from boxes import fetch_segment, frame_at
from keyframes import keyframe_times
from rules import features_for_drive, load_positions, rejection
from session import login

ROOT = Path(__file__).resolve().parents[1]
CLIPS = ROOT / "dashcam" / "out" / "clips.csv"
TRAIL = ROOT / "dashcam" / "out" / "kerb_trail.jsonl"
# Lower than this, the box is far up the road, not along the near kerb.
NEAR_BOTTOM = 0.78
# How far the kerb sits from image centre at the bottom of the frame.
KERB_SPREAD = 0.28
HORIZON = 0.45
# Gaps wider than this many flanking-car widths are not one parking space.
MAX_GAP_CARS = 2.5

TRAIL_FIELDS = (
    "trail_id",
    "space_id",
    "source",
    "video",
    "set",
    "chunk",
    "segment_number",
    "time_sec",
    "side",
    "state",
    "step",
    "detail",
    "reason",
    "confidence",
)


def _norm_box(box: dict, width: int, height: int) -> dict:
    x1, y1, x2, y2 = [float(value) for value in box["bbox"]]
    return {
        "label": box["label"],
        "confidence": box.get("confidence"),
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "cx": (x1 + x2) / 2.0 / width,
        "bottom": y2 / height,
        "w": x2 - x1,
        "h": y2 - y1,
    }


def _side_of(box: dict) -> str | None:
    """Left or right kerb, or None when the box sits in the lane ahead."""
    if box["bottom"] < NEAR_BOTTOM:
        return None
    depth = max(box["bottom"] - HORIZON, 0.05) / (1.0 - HORIZON)
    half = KERB_SPREAD * depth
    if box["cx"] < 0.5 - half:
        return "left"
    if box["cx"] > 0.5 + half:
        return "right"
    return None


def _size_ok(box: dict, peers: list[dict]) -> bool:
    """Box height should match other boxes on this side at a similar depth."""
    similar = [peer for peer in peers if abs(peer["bottom"] - box["bottom"]) <= 0.08 and peer is not box]
    if not similar:
        return box["h"] >= 70 and box["w"] >= 90
    median = sorted(peer["h"] for peer in similar)[len(similar) // 2]
    if median <= 0:
        return False
    ratio = box["h"] / median
    return 0.45 <= ratio <= 2.2


def slots_for_frame(frame: dict, width: int, height: int) -> list[dict]:
    boxes = [_norm_box(box, width, height) for box in frame["boxes"]]
    grouped: dict[str, list[dict]] = {"left": [], "right": []}
    rejected: list[dict] = []
    for box in boxes:
        side = _side_of(box)
        if side is None:
            continue
        if box["label"] == "bus":
            rejected.append(
                {
                    "side": side,
                    "state": "rejected",
                    "reason": "bus_not_a_parked_car",
                    "confidence": box["confidence"],
                    "detail": "bus box on the kerb side is not a parked car",
                    "bbox": [round(box["x1"]), round(box["y1"]), round(box["x2"]), round(box["y2"])],
                }
            )
            continue
        grouped[side].append(box)

    slots: list[dict] = list(rejected)
    for side, cars in grouped.items():
        parked = [car for car in cars if car["label"] in {"car", "truck"} and _size_ok(car, cars)]
        inconsistent = [car for car in cars if car not in parked and car["label"] != "bus"]
        for car in inconsistent:
            slots.append(
                {
                    "side": side,
                    "state": "rejected",
                    "reason": "size_inconsistent_with_distance",
                    "confidence": car["confidence"],
                    "detail": f"height {car['h']:.0f}px does not match neighbours at this depth",
                    "bbox": [round(car["x1"]), round(car["y1"]), round(car["x2"]), round(car["y2"])],
                }
            )
        parked.sort(key=lambda car: car["cx"])
        for car in parked:
            slots.append(
                {
                    "side": side,
                    "state": "occupied",
                    "reason": "",
                    "confidence": car["confidence"],
                    "detail": (
                        f"parked-looking {car['label']} cx={car['cx']:.2f} "
                        f"bottom={car['bottom']:.2f} w={car['w']:.0f}px"
                    ),
                    "bbox": [round(car["x1"]), round(car["y1"]), round(car["x2"]), round(car["y2"])],
                }
            )
        if len(parked) < 2:
            continue
        for left, right in zip(parked, parked[1:]):
            gap_px = right["x1"] - left["x2"]
            car_px = (left["w"] + right["w"]) / 2.0
            if car_px <= 0 or gap_px <= 0:
                continue
            ratio = gap_px / car_px
            if ratio < 1.0:
                slots.append(
                    {
                        "side": side,
                        "state": "rejected",
                        "reason": "shorter_than_one_car_length",
                        "confidence": None,
                        "detail": f"gap {gap_px:.0f}px is {ratio:.2f} of the flanking car width {car_px:.0f}px",
                        "bbox": _gap_box(left, right),
                    }
                )
            elif ratio > MAX_GAP_CARS:
                slots.append(
                    {
                        "side": side,
                        "state": "rejected",
                        "reason": "longer_than_two_and_a_half_car_lengths",
                        "confidence": None,
                        "detail": (
                            f"gap {gap_px:.0f}px is {ratio:.2f} car widths; "
                            "treated as a driveway or a kerb with no parking row"
                        ),
                        "bbox": _gap_box(left, right),
                    }
                )
            else:
                slots.append(
                    {
                        "side": side,
                        "state": "gap",
                        "reason": "",
                        "confidence": None,
                        "detail": f"gap {gap_px:.0f}px is {ratio:.2f} of the flanking car width {car_px:.0f}px",
                        "bbox": _gap_box(left, right),
                    }
                )
    return slots


def _gap_box(left: dict, right: dict) -> list[int]:
    return [
        round(left["x2"]),
        round(min(left["y1"], right["y1"])),
        round(right["x1"]),
        round(max(left["y2"], right["y2"])),
    ]


def _write_kerbdb(rows: list[dict]) -> bool:
    path = ROOT / "common" / "kerbdb.py"
    if not path.is_file():
        return False
    import importlib.util

    spec = importlib.util.spec_from_file_location("kerbdb", path)
    if spec is None or spec.loader is None:
        return False
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    writer = getattr(module, "write_trail", None)
    if writer is None:
        return False
    writer(rows)
    return True


def write_trail(rows: list[dict], path: Path = TRAIL) -> str:
    if _write_kerbdb(rows):
        return "common/kerbdb.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return str(path)


def collect_slots(sets: list[str], clips_csv: Path) -> list[dict]:
    backend, token = login()
    positions = load_positions()
    osm: dict[str, list[dict] | None] = {}
    for set_id in sets:
        osm[set_id] = features_for_drive(set_id, positions) if positions else None
    cache: dict[str, dict] = {}
    records: list[dict] = []
    with clips_csv.open(newline="") as handle:
        segments = [row for row in csv.DictReader(handle) if row["set"] in sets]
    for segment in segments:
        source = segment["segment_source_uri"]
        if source not in cache:
            cache[source] = fetch_segment(backend, token, source)
        payload = cache[source]
        duration = float(segment["end_sec_in_set"]) - float(segment["start_sec_in_set"])
        times = keyframe_times(duration, segment["set"], int(segment["chunk"]))
        lat = lon = None
        if positions is not None:
            found = positions.get((segment["set"], int(segment["chunk"])))
            if found is not None:
                lat, lon = found
        for time_sec in times:
            frame = frame_at(payload["frames"], time_sec)
            for index, slot in enumerate(slots_for_frame(frame, payload["width"], payload["height"])):
                rule_reason, rule_detail = rejection(lat, lon, osm.get(segment["set"]))
                state = slot["state"]
                reason = slot["reason"]
                if state != "rejected" and rule_reason:
                    state = "rejected"
                    reason = rule_reason
                space_id = (
                    f"{segment['set']}_c{int(segment['chunk']):04d}"
                    f"_s{int(segment['segment_number']):02d}_t{time_sec:.1f}_{slot['side']}_{index}"
                )
                records.append(
                    {
                        "space_id": space_id,
                        "source": "dashcam",
                        "video": source,
                        "set": segment["set"],
                        "chunk": int(segment["chunk"]),
                        "segment_number": int(segment["segment_number"]),
                        "time_sec": time_sec,
                        "side": slot["side"],
                        "state": state,
                        "reason": reason,
                        "detail": slot["detail"],
                        "confidence": slot["confidence"],
                        "bbox": slot.get("bbox"),
                        "width": payload["width"],
                        "height": payload["height"],
                        "rule_reason": rule_reason,
                        "rule_detail": rule_detail,
                        "lat": lat,
                        "lon": lon,
                    }
                )
    return records


def trail_steps(record: dict, extra: list[tuple[str, str, str, object]] | None = None) -> list[dict]:
    steps = [
        ("gate", "keyframe kept at 1 per second because data/pie_map.csv has no speed", "", record["confidence"]),
        ("boxes", record["detail"], "", record["confidence"]),
        ("geometry", record["detail"], record["reason"] if record["state"] == "rejected" and record["reason"] != record["rule_reason"] else "", record["confidence"]),
        ("rule", record["rule_detail"], record["rule_reason"], None),
    ]
    if extra:
        steps.extend(extra)
    rows = []
    for step, detail, reason, confidence in steps:
        rows.append(
            {
                "trail_id": f"{record['space_id']}:{step}",
                "space_id": record["space_id"],
                "source": record["source"],
                "video": record["video"],
                "set": record["set"],
                "chunk": record["chunk"],
                "segment_number": record["segment_number"],
                "time_sec": record["time_sec"],
                "side": record["side"],
                "state": record["state"],
                "step": step,
                "detail": detail,
                "reason": reason,
                "confidence": confidence,
            }
        )
    return rows


def build(sets: list[str], clips_csv: Path, trail_path: Path) -> dict:
    records = collect_slots(sets, clips_csv)
    rows_out: list[dict] = []
    for record in records:
        rows_out.extend(trail_steps(record))
    destination = write_trail(rows_out, trail_path)
    return {"trail_rows": len(rows_out), "slots": len(records), "wrote": destination}


def main() -> None:
    parser = argparse.ArgumentParser(description="Write kerb slots and their trail")
    parser.add_argument("--sets", nargs="+", default=["set01", "set02"])
    parser.add_argument("--clips", type=Path, default=CLIPS)
    parser.add_argument("--trail", type=Path, default=TRAIL)
    args = parser.parse_args()
    summary = build(args.sets, args.clips, args.trail)
    print(json.dumps(summary), file=sys.stderr)


if __name__ == "__main__":
    main()
