#!/usr/bin/env python3
"""Mark parked cars on neighborhood_cam-1 and write an occupancy timeline.

Occupancy is decided by POSITION, not by track id. Night detections flicker,
so one parked car splits into many short tracks; a per-track still test
misses it. Instead:

1. Every vehicle box is assigned to exactly one area by its bottom centre, in
   this priority: driveway, kerb strip, lane, else off_street.
2. Each kerb strip is cut into ~1 m cells along its centreline. In a frame, a
   cell is covered when the bottom edge of a vehicle box whose bottom centre is
   in that strip spans it (projected onto the centreline).
3. Per cell, coverage is the share of frames covered (20 fps sidecars, counted
   per second). A cell is occupied during any 120 s window in which it is
   covered in at least 60% of frames (trimmed to the first and last covered
   second of that window). Occupied cells next to each other are
   merged into occupied intervals; a connected run in time and along the kerb
   is one parked period.
4. Tracks of a vehicle label where one ends and another starts up to 3 s
   later within 30 px (closer bottom corner, see box_gap_px) are stitched
   into one group.
5. A track is parked when its stitched group spends at least 120 s in total
   on occupied cells, or when at least half of its own boxes sit on a parked
   period (a run of occupied cells lasting at least 120 s). The second rule
   catches night fragments that stitching cannot join (box sides swing with
   glare and ids restart). Otherwise its majority area decides: kerb strip or
   lane is traffic; driveway or off_street is not_space.

Output files keep the earlier formats (extra keys only):
  occupancy_timeline.jsonl, track_states.jsonl, occupancy_summary.json
plus parked_periods.jsonl (one row per parked period).
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path


STEP_S = 10.0
FPS = 20.0
OWN_FRACTION = 0.5


# ---------------------------------------------------------------- geometry

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


def mean_box(boxes):
    n = len(boxes)
    acc = [0.0, 0.0, 0.0, 0.0]
    for b in boxes:
        for i in range(4):
            acc[i] += b[i]
    return [v / n for v in acc]


def area_of(x, y, kerb):
    """Exactly one area per point: (kind, id)."""
    for d in kerb["driveways"]:
        if point_in_poly(x, y, d["polygon"]):
            return "driveway", d["id"]
    for s in kerb["kerb_strips"]:
        if point_in_poly(x, y, s["polygon"]):
            return "kerb", s["id"]
    if point_in_poly(x, y, kerb["lane"]["polygon"]):
        return "lane", "lane"
    return "off_street", None


class Polyline:
    """Centreline with metres along it; scale may vary per vertex."""

    def __init__(self, pts, px_per_m, ppm_vertices=None):
        self.pts = [tuple(map(float, p)) for p in pts]
        ppm = ppm_vertices or [float(px_per_m)] * len(self.pts)
        self.seg_ppm = [(a + b) * 0.5 for a, b in zip(ppm, ppm[1:])]
        self.cum = [0.0]
        for (a, b), s in zip(zip(self.pts, self.pts[1:]), self.seg_ppm):
            self.cum.append(self.cum[-1] + math.dist(a, b) / s)
        self.length_m = self.cum[-1]

    def to_m(self, px, py):
        best_d, best_m = None, None
        for i in range(len(self.pts) - 1):
            ax, ay = self.pts[i]
            bx, by = self.pts[i + 1]
            vx, vy = bx - ax, by - ay
            seg2 = vx * vx + vy * vy
            if seg2 <= 0:
                continue
            u = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / seg2))
            qx, qy = ax + u * vx, ay + u * vy
            d = (qx - px) ** 2 + (qy - py) ** 2
            if best_d is None or d < best_d:
                best_d = d
                best_m = self.cum[i] + (seg2 ** 0.5) * u / self.seg_ppm[i]
        return best_m

    def to_px(self, m):
        m = max(0.0, min(self.length_m, m))
        for i in range(len(self.pts) - 1):
            if m <= self.cum[i + 1] or i == len(self.pts) - 2:
                span = self.cum[i + 1] - self.cum[i]
                u = 0.0 if span <= 0 else (m - self.cum[i]) / span
                ax, ay = self.pts[i]
                bx, by = self.pts[i + 1]
                return ax + u * (bx - ax), ay + u * (by - ay)
        return self.pts[-1]

    def interval(self, box):
        a = self.to_m(box[0], box[3])
        b = self.to_m(box[2], box[3])
        if a is None or b is None:
            return None
        a, b = min(a, b), max(a, b)
        a = max(0.0, min(self.length_m, a))
        b = max(0.0, min(self.length_m, b))
        return [a, b]


def prepare(kerb):
    occ = kerb["occupancy"]
    strips = {}
    for s in kerb["kerb_strips"]:
        line = Polyline(s["centerline"], s["px_per_m"], s.get("px_per_m_vertices"))
        n_cells = max(1, int(round(line.length_m / float(occ["cell_m"]))))
        strips[s["id"]] = {"id": s["id"], "line": line, "n_cells": n_cells,
                           "cell_m": line.length_m / n_cells}
    stretches = []
    for raw in kerb["legal_stretches"]:
        line = Polyline(raw["centerline"], raw["px_per_m"], raw.get("px_per_m_vertices"))
        stretches.append({**raw, "line": line, "length_m": line.length_m})
    return strips, stretches


# ---------------------------------------------------------------- intervals

def merge_intervals(items):
    cuts = sorted((a, b) for a, b in items if b > a)
    merged = []
    for a, b in cuts:
        if not merged or a > merged[-1][1] + 1e-9:
            merged.append([a, b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    return merged


def subtract_intervals(length, occupied):
    merged = merge_intervals([(max(0.0, a), min(length, b)) for a, b in occupied])
    free, cursor = [], 0.0
    for a, b in merged:
        if a - cursor >= 0.05:
            free.append([cursor, a])
        cursor = max(cursor, b)
    if length - cursor >= 0.05:
        free.append([cursor, length])
    return merged, free


def car_slots(gaps, gap_m):
    return sum(int((b - a) / gap_m) for a, b in gaps)


# ---------------------------------------------------------------- tracks

def load_tracks(path: Path):
    rows = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


STITCH_OVERLAP_S = 0.5


def box_gap_px(a, b):
    """Distance between two boxes: the closer of the two bottom corners.

    At night one side of a box swings with headlight glare (the same parked
    car's box right edge moves between x=571 and x=770 on 20260902) while the
    other side and the bottom stay put, so the box centre jumps by up to
    120 px. Matching on the steadier bottom corner keeps the 30 px test.
    """
    left = math.hypot(a[0] - b[0], a[3] - b[3])
    right = math.hypot(a[2] - b[2], a[3] - b[3])
    return min(left, right)


def stitch(tracks, gap_s, gap_px, vehicle_labels):
    """Union tracks whose end and next start are within gap_s and gap_px.

    Each vehicle track links to its closest successor that starts within
    gap_s after it ends (or up to 0.5 s before, a duplicate id on the same
    car) and whose first box is within gap_px of the last box (box_gap_px).
    """
    parent = {t["track_id"]: t["track_id"] for t in tracks}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    veh = sorted((t for t in tracks if t["label"] in vehicle_labels), key=lambda t: t["start_time_s"])
    for a in veh:
        ea = a["box_path"][-1]
        best = None
        for b in veh:
            if b is a or b["start_time_s"] < a["start_time_s"]:
                continue
            dt = b["start_time_s"] - a["end_time_s"]
            if dt < -STITCH_OVERLAP_S or dt > gap_s:
                continue
            d = box_gap_px(ea, b["box_path"][0])
            if d <= gap_px and (best is None or (dt, d) < best[0]):
                best = ((dt, d), b)
        if best is not None:
            ra, rb = find(a["track_id"]), find(best[1]["track_id"])
            if ra != rb:
                parent[rb] = ra
    return {t["track_id"]: find(t["track_id"]) for t in tracks}


# ---------------------------------------------------------------- occupancy

def day_occupancy(tracks, kerb, strips):
    occ = kerb["occupancy"]
    vehicle = set(occ["vehicle_labels"])
    win = int(occ["window_s"])
    need = float(occ["min_fraction"])
    t_end = max(t["end_time_s"] for t in tracks)
    n_sec = int(math.floor(t_end)) + 1
    # covered[strip][cell] -> per-second set of covered frame indices
    covered = {sid: [defaultdict(set) for _ in range(s["n_cells"])] for sid, s in strips.items()}
    for t in tracks:
        if t["label"] not in vehicle:
            continue
        for fr, box in zip(t["frames"], t["box_path"]):
            kind, aid = area_of(*bottom_center(box), kerb)
            if kind != "kerb":
                continue
            s = strips[aid]
            iv = s["line"].interval(box)
            if iv is None:
                continue
            c0 = int(iv[0] // s["cell_m"])
            c1 = min(s["n_cells"] - 1, int(iv[1] // s["cell_m"]))
            ts = fr["time_s"]
            sec = int(ts)
            fidx = int(round(ts * FPS))
            for c in range(c0, c1 + 1):
                covered[aid][c][sec].add(fidx)
    occupied = {}
    for sid, s in strips.items():
        grid = [[False] * n_sec for _ in range(s["n_cells"])]
        for c in range(s["n_cells"]):
            cov = [min(1.0, len(covered[sid][c].get(sec, ())) / FPS) for sec in range(n_sec)]
            run = sum(cov[:win])
            for w in range(0, n_sec - win + 1):
                if w > 0:
                    run += cov[w + win - 1] - cov[w - 1]
                if run / win >= need:
                    # occupied from the first to the last covered second of
                    # the window, so the period is not smeared past the car
                    covered_secs = [sec for sec in range(w, w + win) if cov[sec] > 0]
                    for sec in range(covered_secs[0], covered_secs[-1] + 1):
                        grid[c][sec] = True
        occupied[sid] = grid
    return occupied, n_sec


def parked_periods(day, occupied, strips, min_s):
    """Connected components over (cell, second) per strip.

    Returns the periods and long_grid: the occupied cell-seconds that belong
    to a period lasting at least min_s.
    """
    periods = []
    long_grid = {sid: [[False] * len(g[0]) for _ in g] if g else [] for sid, g in occupied.items()}
    for sid, grid in occupied.items():
        s = strips[sid]
        n_c = len(grid)
        n_s = len(grid[0]) if grid else 0
        seen = [[False] * n_s for _ in range(n_c)]
        for c in range(n_c):
            for sec in range(n_s):
                if not grid[c][sec] or seen[c][sec]:
                    continue
                stack = [(c, sec)]
                seen[c][sec] = True
                cells, secs, members = set(), set(), []
                while stack:
                    cc, ss = stack.pop()
                    members.append((cc, ss))
                    cells.add(cc)
                    secs.add(ss)
                    for dc, ds in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        nc, ns = cc + dc, ss + ds
                        if 0 <= nc < n_c and 0 <= ns < n_s and grid[nc][ns] and not seen[nc][ns]:
                            seen[nc][ns] = True
                            stack.append((nc, ns))
                if max(secs) + 1 - min(secs) >= min_s:
                    for cc, ss in members:
                        long_grid[sid][cc][ss] = True
                periods.append({
                    "day": day,
                    "kerb_id": sid,
                    "t_start_s": float(min(secs)),
                    "t_end_s": float(max(secs) + 1),
                    "from_m": round(min(cells) * s["cell_m"], 3),
                    "to_m": round((max(cells) + 1) * s["cell_m"], 3),
                    "n_cells": len(cells),
                })
    periods.sort(key=lambda p: (p["kerb_id"], p["t_start_s"]))
    for i, p in enumerate(periods, 1):
        p["period_id"] = f"{day}-P{i:02d}"
        p["duration_s"] = p["t_end_s"] - p["t_start_s"]
        p["long_enough"] = p["duration_s"] >= min_s
    return periods, long_grid


def classify_day(day, tracks, kerb, strips, occupied, long_grid, n_sec):
    occ = kerb["occupancy"]
    vehicle = set(occ["vehicle_labels"])
    group_of = stitch(tracks, float(occ["stitch_gap_s"]), float(occ["stitch_px"]), vehicle)
    members = defaultdict(list)
    for t in tracks:
        members[group_of[t["track_id"]]].append(t)

    def on_occupied(box, ts, grid=occupied):
        kind, aid = area_of(*bottom_center(box), kerb)
        if kind != "kerb":
            return None
        s = strips[aid]
        iv = s["line"].interval(box)
        sec = int(ts)
        if iv is None or sec >= n_sec:
            return None
        c0 = int(iv[0] // s["cell_m"])
        c1 = min(s["n_cells"] - 1, int(iv[1] // s["cell_m"]))
        if any(grid[aid][c][sec] for c in range(c0, c1 + 1)):
            return aid
        return None

    group_info = {}
    for gid, ts in members.items():
        obs = sorted(
            (fr["time_s"], box) for t in ts for fr, box in zip(t["frames"], t["box_path"])
        )
        overlap = 0.0
        span = None
        kerbs = Counter()
        prev = None
        for tsec, box in obs:
            hit = on_occupied(box, tsec) if ts[0]["label"] in vehicle else None
            if hit is not None:
                kerbs[hit] += 1
                if prev is not None and prev[1] is not None:
                    dt = tsec - prev[0]
                    if 0 < dt <= float(occ["stitch_gap_s"]):
                        overlap += dt
                span = (span[0] if span else tsec, tsec)
            prev = (tsec, hit)
        group_info[gid] = {"overlap_s": overlap, "span": span,
                           "kerb_id": kerbs.most_common(1)[0][0] if kerbs else None,
                           "n_tracks": len(ts)}

    rows = []
    need = float(occ["parked_overlap_s"])
    for t in tracks:
        gid = group_of[t["track_id"]]
        g = group_info[gid]
        areas = Counter(area_of(*bottom_center(b), kerb) for b in t["box_path"])
        (kind, aid), _ = areas.most_common(1)[0]
        box = mean_box([b for b in t["box_path"] if area_of(*bottom_center(b), kerb) == (kind, aid)])
        bx, by = bottom_center(box)
        own = 0
        if t["label"] in vehicle:
            own = sum(1 for fr, b in zip(t["frames"], t["box_path"])
                      if on_occupied(b, fr["time_s"], long_grid) is not None)
        own_frac = own / len(t["box_path"])
        if t["label"] in vehicle and g["overlap_s"] >= need:
            state, why = "parked", "stitched track on occupied cells >= 120 s"
        elif t["label"] in vehicle and own_frac >= OWN_FRACTION:
            state, why = "parked", "most boxes on a parked period >= 120 s"
        elif kind in ("kerb", "lane"):
            state, why = "traffic", f"mostly in {kind}, not on a parked period"
        else:
            state, why = "not_space", f"mostly in {kind}" + (f" {aid}" if aid else "")
        rows.append({
            "track_id": t["track_id"],
            "day": t["day"],
            "label": t["label"],
            "state": state,
            "still_run_s": g["overlap_s"],
            "still_span_s": list(g["span"]) if (state == "parked" and g["span"]) else None,
            "bottom_center": [bx, by],
            "kerb_id": g["kerb_id"] if state == "parked" else (aid if kind == "kerb" else None),
            "driveway_id": aid if kind == "driveway" else None,
            "in_lane": kind == "lane",
            "box": box,
            "start_time_s": t["start_time_s"],
            "end_time_s": t["end_time_s"],
            "area": kind,
            "group_id": gid,
            "group_n_tracks": g["n_tracks"],
            "occupied_overlap_s": round(g["overlap_s"], 2),
            "share_on_parked_period": round(own_frac, 3),
            "why": why,
        })
    return rows


def timeline_for_day(day, rows, stretches, strips, occupied, n_sec, kerb):
    gap_m = float(kerb["free_gap_m"])
    t_end = max(r["end_time_s"] for r in rows) if rows else 0.0
    parked = [r for r in rows if r["state"] == "parked"]
    out = []
    t = 0.0
    while t <= t_end + 1e-6:
        sec = min(int(t), n_sec - 1)
        for st in stretches:
            s = strips[st["kerb_id"]]
            grid = occupied[st["kerb_id"]]
            cells = [c for c in range(s["n_cells"]) if grid[c][sec]]
            pieces = merge_intervals([(c * s["cell_m"], (c + 1) * s["cell_m"]) for c in cells])
            projected = []
            for a, b in pieces:
                pa = st["line"].to_m(*s["line"].to_px(a))
                pb = st["line"].to_m(*s["line"].to_px(b))
                lo, hi = min(pa, pb), max(pa, pb)
                if hi - lo >= 0.05:
                    projected.append((lo, hi))
            occ, free = subtract_intervals(st["length_m"], projected)
            ids = [r["track_id"] for r in parked
                   if r["kerb_id"] == st["kerb_id"] and r["start_time_s"] <= t <= r["end_time_s"]]
            out.append({
                "day": day,
                "time_s": round(t, 3),
                "stretch_id": st["id"],
                "kerb_id": st["kerb_id"],
                "length_m": st["length_m"],
                "occupied_m": [[round(a, 3), round(b, 3)] for a, b in occ],
                "free_m": [[round(a, 3), round(b, 3)] for a, b in free],
                "free_spaces": car_slots(free, gap_m),
                "parked_track_ids": ids,
            })
        t += STEP_S
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kerb", type=Path, required=True)
    parser.add_argument("--tracks", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    kerb = json.loads(args.kerb.read_text())
    strips, stretches = prepare(kerb)
    by_day = defaultdict(list)
    for path in args.tracks:
        for rec in load_tracks(path):
            by_day[rec["day"]].append(rec)

    classified, timeline, periods, counts = [], [], [], {}
    for day in sorted(by_day):
        tracks = by_day[day]
        occupied, n_sec = day_occupancy(tracks, kerb, strips)
        day_periods, long_grid = parked_periods(day, occupied, strips, float(kerb["occupancy"]["parked_overlap_s"]))
        rows = classify_day(day, tracks, kerb, strips, occupied, long_grid, n_sec)
        day_tl = timeline_for_day(day, rows, stretches, strips, occupied, n_sec, kerb)
        classified.extend(rows)
        timeline.extend(day_tl)
        periods.extend(day_periods)
        states = Counter(r["state"] for r in rows)
        areas = Counter(r["area"] for r in rows)
        counts[day] = {
            "n_tracks": len(rows),
            "states": dict(states),
            "areas": dict(areas),
            "parked_track_ids": [r["track_id"] for r in rows if r["state"] == "parked"],
            "n_stitched_groups": len({r["group_id"] for r in rows}),
            "n_parked_periods": sum(1 for p in day_periods if p["long_enough"]),
            "n_occupied_runs_all": len(day_periods),
            "parked_periods": [
                {k: p[k] for k in ("period_id", "kerb_id", "t_start_s", "t_end_s", "duration_s", "from_m", "to_m", "long_enough")}
                for p in day_periods
            ],
            "n_timeline_rows": len(day_tl),
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as fh:
        for row in timeline:
            fh.write(json.dumps(row) + "\n")
    with args.out.with_name("track_states.jsonl").open("w") as fh:
        for row in classified:
            fh.write(json.dumps(row) + "\n")
    with args.out.with_name("parked_periods.jsonl").open("w") as fh:
        for row in periods:
            fh.write(json.dumps(row) + "\n")
    summary = {
        "method": "position-based cell occupancy (see parked.py docstring)",
        "occupancy": kerb["occupancy"],
        "still_s": kerb["still_s"],
        "free_gap_m": kerb["free_gap_m"],
        "step_s": STEP_S,
        "stretches": {s["id"]: {"length_m": round(s["length_m"], 3), "px_per_m": s["px_per_m"]} for s in stretches},
        "kerb_cells": {sid: {"length_m": round(s["line"].length_m, 3), "n_cells": s["n_cells"],
                             "cell_m": round(s["cell_m"], 3)} for sid, s in strips.items()},
        "days": counts,
    }
    args.out.with_name("occupancy_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({d: {"states": c["states"], "parked_periods": c["n_parked_periods"],
                          "n_parked_tracks": len(c["parked_track_ids"])} for d, c in counts.items()}))


if __name__ == "__main__":
    main()
