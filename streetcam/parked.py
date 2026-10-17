#!/usr/bin/env python3
"""Mark parked cars on neighborhood_cam-1 and write an occupancy timeline.

A track is parked only when its longest continuous still run is longer than
the light cycle (120 s) and the bottom centre of the box during that run sits
in a kerb strip. A bottom centre inside a driveway is not_space. A bottom
centre in the lane, or on the kerb but not still for 120 s, is traffic.

Occupied intervals are the parked car's footprint projected onto the legal
kerb centreline. Free length is the legal centreline minus those intervals.
A free space counts when a free gap is at least free_gap_m (5.5).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


STILL_WINDOW_PX = 25.0
STILL_MAX_DT_S = 1.0
STEP_S = 10.0


def point_in_poly(x: float, y: float, poly) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if yi != yj and ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def center(box):
    return (box[0] + box[2]) * 0.5, (box[1] + box[3]) * 0.5


def bottom_center(box):
    return (box[0] + box[2]) * 0.5, box[3]


def longest_still(frames, boxes):
    """Longest run whose box centres stay inside a 25 px square.

    Track 20260901-0002 sits in one place for the whole day (centre range
    about 10 by 8 px) but single-frame jitter exceeds a 20 px/s speed test.
    The window matches that measured wobble. A gap over 1 s starts a new run.
    """
    from collections import deque

    segs = [[]]
    prev_t = None
    for fr, box in zip(frames, boxes):
        t = fr["time_s"]
        if prev_t is not None and (t - prev_t <= 0 or t - prev_t > STILL_MAX_DT_S):
            segs.append([])
        segs[-1].append((t, *center(box)))
        prev_t = t
    best = 0.0
    best_span = None
    for seg in segs:
        if len(seg) < 2:
            continue
        maxx, minx, maxy, miny = deque(), deque(), deque(), deque()
        start = 0
        for i, (t, x, y) in enumerate(seg):
            while maxx and seg[maxx[-1]][1] <= x:
                maxx.pop()
            while minx and seg[minx[-1]][1] >= x:
                minx.pop()
            while maxy and seg[maxy[-1]][2] <= y:
                maxy.pop()
            while miny and seg[miny[-1]][2] >= y:
                miny.pop()
            maxx.append(i)
            minx.append(i)
            maxy.append(i)
            miny.append(i)
            while (
                seg[maxx[0]][1] - seg[minx[0]][1] > STILL_WINDOW_PX
                or seg[maxy[0]][2] - seg[miny[0]][2] > STILL_WINDOW_PX
            ):
                if maxx[0] == start:
                    maxx.popleft()
                if minx[0] == start:
                    minx.popleft()
                if maxy[0] == start:
                    maxy.popleft()
                if miny[0] == start:
                    miny.popleft()
                start += 1
            dur = t - seg[start][0]
            if dur > best:
                best = dur
                best_span = (seg[start][0], t)
    return best, best_span


def mean_box(boxes):
    n = len(boxes)
    acc = [0.0, 0.0, 0.0, 0.0]
    for b in boxes:
        for i in range(4):
            acc[i] += b[i]
    return [v / n for v in acc]


def boxes_in_span(frames, boxes, span):
    t0, t1 = span
    chosen = [b for fr, b in zip(frames, boxes) if t0 <= fr["time_s"] <= t1]
    return chosen or boxes


def project_interval(box, stretch):
    """Metres along the centreline covered by the box bottom edge."""
    pts = stretch["centerline"]
    cum = stretch["cum_m"]
    px_per_m = stretch["px_per_m"]
    targets = [(box[0], box[3]), (box[2], box[3])]
    metres = []
    for px, py in targets:
        best_d = None
        best_m = None
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            vx, vy = bx - ax, by - ay
            seg2 = vx * vx + vy * vy
            if seg2 <= 0:
                continue
            u = ((px - ax) * vx + (py - ay) * vy) / seg2
            u = max(0.0, min(1.0, u))
            qx, qy = ax + u * vx, ay + u * vy
            d = (qx - px) ** 2 + (qy - py) ** 2
            if best_d is None or d < best_d:
                best_d = d
                seg_px = (seg2 ** 0.5) * u
                best_m = cum[i] + seg_px / px_per_m
        if best_m is not None:
            metres.append(best_m)
    if len(metres) < 2:
        return None
    a, b = min(metres), max(metres)
    length = stretch["length_m"]
    a = max(0.0, min(length, a))
    b = max(0.0, min(length, b))
    if b - a < 0.05:
        return None
    return [a, b]


def subtract_intervals(length, occupied):
    """occupied is a list of [a, b] inside [0, length]. Return free pieces."""
    cuts = sorted((max(0.0, a), min(length, b)) for a, b in occupied if b > a)
    merged = []
    for a, b in cuts:
        if not merged or a > merged[-1][1]:
            merged.append([a, b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    free = []
    cursor = 0.0
    for a, b in merged:
        if a - cursor >= 0.05:
            free.append([cursor, a])
        cursor = max(cursor, b)
    if length - cursor >= 0.05:
        free.append([cursor, length])
    return merged, free


def car_slots(gaps, gap_m):
    return sum(int((b - a) / gap_m) for a, b in gaps)


def prepare_stretches(kerb):
    stretches = []
    for raw in kerb["legal_stretches"]:
        pts = raw["centerline"]
        ppm = float(raw["px_per_m"])
        cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            dist = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 / ppm
            cum.append(cum[-1] + dist)
        stretches.append({**raw, "cum_m": cum, "length_m": cum[-1]})
    return stretches


def classify_track(rec, kerb):
    still_s, span = longest_still(rec["frames"], rec["box_path"])
    if span is None:
        box = rec["mean_box"]
    else:
        box = mean_box(boxes_in_span(rec["frames"], rec["box_path"], span))
    bx, by = bottom_center(box)
    driveway = None
    for d in kerb["driveways"]:
        if point_in_poly(bx, by, d["polygon"]):
            driveway = d["id"]
            break
    kerb_id = None
    for strip in kerb["kerb_strips"]:
        if point_in_poly(bx, by, strip["polygon"]):
            kerb_id = strip["id"]
            break
    in_lane = point_in_poly(bx, by, kerb["lane"]["polygon"])
    need = float(kerb["still_s"])
    if driveway is not None:
        state = "not_space"
    elif kerb_id is not None and still_s > need:
        state = "parked"
    elif in_lane or kerb_id is not None:
        state = "traffic"
    else:
        state = "not_space"
    return {
        "track_id": rec["track_id"],
        "day": rec["day"],
        "label": rec["label"],
        "state": state,
        "still_run_s": still_s,
        "still_span_s": list(span) if span else None,
        "bottom_center": [bx, by],
        "kerb_id": kerb_id,
        "driveway_id": driveway,
        "in_lane": in_lane,
        "box": box,
        "start_time_s": rec["start_time_s"],
        "end_time_s": rec["end_time_s"],
    }


def load_tracks(path: Path):
    rows = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def timeline_for_day(day, classified, stretches, kerb):
    parked = [c for c in classified if c["state"] == "parked" and c["still_span_s"]]
    if classified:
        t_end = max(c["end_time_s"] for c in classified)
    else:
        t_end = 0.0
    gap_m = float(kerb["free_gap_m"])
    rows = []
    t = 0.0
    while t <= t_end + 1e-6:
        for stretch in stretches:
            occupied = []
            ids = []
            for car in parked:
                t0, t1 = car["still_span_s"]
                if not (t0 <= t <= t1):
                    continue
                if car["kerb_id"] != stretch["kerb_id"]:
                    continue
                interval = project_interval(car["box"], stretch)
                if interval is None:
                    continue
                occupied.append(interval)
                ids.append(car["track_id"])
            occ, free = subtract_intervals(stretch["length_m"], occupied)
            rows.append(
                {
                    "day": day,
                    "time_s": round(t, 3),
                    "stretch_id": stretch["id"],
                    "kerb_id": stretch["kerb_id"],
                    "length_m": stretch["length_m"],
                    "occupied_m": [[round(a, 3), round(b, 3)] for a, b in occ],
                    "free_m": [[round(a, 3), round(b, 3)] for a, b in free],
                    "free_spaces": car_slots(free, gap_m),
                    "parked_track_ids": ids,
                }
            )
        t += STEP_S
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kerb", type=Path, required=True)
    parser.add_argument("--tracks", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    kerb = json.loads(args.kerb.read_text())
    stretches = prepare_stretches(kerb)
    classified = []
    for path in args.tracks:
        for rec in load_tracks(path):
            classified.append(classify_track(rec, kerb))
    by_day = {}
    for row in classified:
        by_day.setdefault(row["day"], []).append(row)
    timeline = []
    for day in sorted(by_day):
        timeline.extend(timeline_for_day(day, by_day[day], stretches, kerb))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as fh:
        for row in timeline:
            fh.write(json.dumps(row) + "\n")
    counts = {}
    for day, rows in by_day.items():
        states = {}
        for row in rows:
            states[row["state"]] = states.get(row["state"], 0) + 1
        parked_ids = [r["track_id"] for r in rows if r["state"] == "parked"]
        day_rows = [r for r in timeline if r["day"] == day]
        counts[day] = {
            "n_tracks": len(rows),
            "states": states,
            "parked_track_ids": parked_ids,
            "n_timeline_rows": len(day_rows),
            "stretches": {
                s["id"]: {"length_m": round(s["length_m"], 3), "px_per_m": s["px_per_m"]}
                for s in stretches
            },
        }
    summary = {
        "still_s": kerb["still_s"],
        "free_gap_m": kerb["free_gap_m"],
        "step_s": STEP_S,
        "stretches": counts[next(iter(counts))]["stretches"] if counts else {},
        "days": counts,
    }
    summary_path = args.out.with_name("occupancy_summary.json")
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    labels_path = args.out.with_name("track_states.jsonl")
    with labels_path.open("w") as fh:
        for row in classified:
            fh.write(json.dumps(row) + "\n")
    print(json.dumps({"days": {d: counts[d]["states"] for d in counts}, "parked": {d: counts[d]["parked_track_ids"] for d in counts}}))


if __name__ == "__main__":
    main()
