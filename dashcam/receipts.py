"""Dashcam kerb receipts for the Toronto PIE drives (PLAN.md phase 1, steps 2-8).

Stages per drive (set01..set06), every step written to kerb_trail:
  1 gate      keyframe every ~8 m of travel (segment speed/distance from data/pie_segments.csv), else 1 per second
  2 boxes     stored YOLO boxes for that frame (car/truck/bus, conf >= 0.4), original 1920x1080 pixels
  3 geometry  left/right kerb rows, lane check, gaps between parked boxes >= one car length (from box widths)
  4 rule      OSM junctions, driveways, crossings, signals, hydrants, bus stops near a candidate gap
  5 cosmos    only for gaps the rules cannot settle (local Cosmos-Reason server), one word + confidence
  6 position  GPS interpolated at the keyframe time + forward/side offset
  7 write     kerb_spaces / kerb_observations / kerb_trail via common/kerbdb.py, plus dashcam/out/<set>.jsonl

Usage (repo root):
  python dashcam/receipts.py slots set04            # stages 1-4, writes dashcam/out/work/set04_slots.json
  python dashcam/receipts.py cosmos set04           # stage 5 on the queued gaps (needs frames, see frames.py)
  python dashcam/receipts.py write set04            # stages 6-7
"""
import csv, json, math, os, pathlib, sys, time, collections, datetime, base64

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
DATA = pathlib.Path(os.environ.get("DASHCAM_DATA", r"C:\Users\samar\bystander-data"))
DET_DIR = DATA / "dashcam-det"
OSM_DIR = DATA / "dashcam-osm"
FRAME_DIR = DATA / "dashcam-frames"
OUT = REPO / "dashcam" / "out"
WORK = OUT / "work"
COSMOS_URL = os.environ.get("COSMOS_LOCAL_URL", "http://127.0.0.1:8850")

# ---- parameters (all assumptions are listed here and in RESULTS.md) ----
GATE_M = 8.0                 # keyframe spacing in metres of travel
VEHICLES = {"car", "truck", "bus"}
MIN_CONF = 0.4
CX = 960.0                   # image centre column (1920 px wide)
CAR_W_M = 1.8                # car width used to calibrate pixels-per-metre from rear-view boxes
FOCAL_PX = 1000.0            # assumed focal length (px) for forward distance; only used for pin offset
MAX_Z_M = 40.0
EGO_HALF_M = 1.5             # ego travel band half-width ("clearly in our lane")
EGO_OVERLAP = 0.3            # box covers >= 30% of the ego band at its depth -> traffic
GAP_MIN_RATIO = 1.1          # gap >= 1.1 x neighbouring parked-box width (~5.5 m) -> candidate space
GAP_SHORT_RATIO = 0.6        # gaps 0.6-1.1 x are recorded as "shorter than a car length"
GAP_CLEAR_MAX = 3.0          # longer gaps are unclear (driveway, no-stopping zone, hidden cars?)
CAR_LEN_M = 5.0              # metres per neighbouring-box width, for the length estimate only
COSMOS_SHARE = 0.05          # at most 5% of slots per drive sent to the model
COSMOS_MIN_P = 0.85          # below this first-token probability -> cannot_tell
RULE_RADII = {"junction": 12.0, "driveway": 6.0, "crossing": 10.0, "traffic_signals": 15.0,
              "fire_hydrant": 5.0, "bus_stop": 15.0, "stop": 10.0}
ROAD = {"motorway", "trunk", "primary", "secondary", "tertiary", "residential", "unclassified", "living_street",
        "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fnum(v, default=None):
    try:
        return float(v) if v not in ("", None) else default
    except ValueError:
        return default


def load_segments(set_name):
    geo = {r["segment_source"]: r for r in csv.DictReader(open(REPO / "data" / "video_geo.csv", encoding="utf-8"))}
    rows = [r for r in csv.DictReader(open(REPO / "data" / "pie_segments.csv", encoding="utf-8")) if r["set"] == set_name]
    rows.sort(key=lambda r: (int(r["chunk"]), int(r["segment"])))
    for r in rows:
        g = geo.get(r["segment_source"], {})
        r["camera_id"] = g.get("camera_id") or "pie_cam-3"
        r["original_video"] = g.get("original_video") or ""
        r["segment_start_sec"] = fnum(g.get("segment_start_sec"), (int(r["segment"]) - 1) * 5.0)
    return rows


def det_path(src):
    return DET_DIR / (src.rsplit("/", 1)[-1].rsplit(".", 1)[0] + ".json")


# ---------------- OSM ----------------
class OSM:
    def __init__(self, set_name):
        self.ok = False
        self.points = []           # (lat, lon, kind, osm_id)
        self.oneway = {}           # street name -> bool
        self.lanes = {}            # street name -> int or None
        self.highway = {}          # street name -> highway class
        p = OSM_DIR / f"{set_name}.json"
        if not p.exists():
            return
        els = json.loads(p.read_text(encoding="utf-8")).get("elements", [])
        nodes = {e["id"]: e for e in els if e["type"] == "node"}
        use = collections.defaultdict(set)
        votes = collections.defaultdict(collections.Counter)
        for e in els:
            if e["type"] != "way":
                continue
            t = e.get("tags", {})
            hw = t.get("highway", "")
            kind = "road" if hw in ROAD else ("service" if hw == "service" else None)
            if not kind:
                continue
            for n in e.get("nodes", []):
                use[n].add((kind, e["id"]))
            name = t.get("name")
            if name and kind == "road":
                votes[name]["oneway"] += 1 if t.get("oneway") in ("yes", "1", "true") else 0
                votes[name]["n"] += 1
                if t.get("lanes", "").isdigit():
                    votes[name]["lanes_" + t["lanes"]] += 1
                votes[name]["hw_" + hw] += 1
        for name, c in votes.items():
            self.oneway[name] = c["oneway"] * 2 > c["n"]
            ln = [(v, int(k[6:])) for k, v in c.items() if k.startswith("lanes_")]
            self.lanes[name] = max(ln)[1] if ln else None
            hws = [(v, k[3:]) for k, v in c.items() if k.startswith("hw_")]
            self.highway[name] = max(hws)[1] if hws else None
        for nid, ways in use.items():
            n = nodes.get(nid)
            if not n or "lat" not in n:
                continue
            roads = {w for k, w in ways if k == "road"}
            services = {w for k, w in ways if k == "service"}
            if len(roads) >= 2:
                self.points.append((n["lat"], n["lon"], "junction", nid))
            elif roads and services:
                self.points.append((n["lat"], n["lon"], "driveway", nid))
        for e in els:
            if e["type"] != "node" or "tags" not in e:
                continue
            t = e["tags"]
            kinds = []
            hw = t.get("highway")
            if hw in ("traffic_signals", "stop"):
                kinds.append(hw)
            if hw == "crossing" or "crossing" in t:
                kinds.append("crossing")
            if hw == "bus_stop" or t.get("public_transport") in ("platform", "stop_position") or t.get("railway") == "tram_stop":
                kinds.append("bus_stop")
            if t.get("emergency") == "fire_hydrant":
                kinds.append("fire_hydrant")
            for k in set(kinds):
                self.points.append((e["lat"], e["lon"], k, e["id"]))
        self.grid = collections.defaultdict(list)
        for p_ in self.points:
            self.grid[(int(p_[0] / 0.0005), int(p_[1] / 0.0005))].append(p_)
        self.ok = True

    def near(self, lat, lon):
        """Rule hits for a candidate position: list of (kind, distance_m, osm_id), nearest first."""
        from common.kerbdb import haversine_m
        gi, gj = int(lat / 0.0005), int(lon / 0.0005)
        hits = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for la, lo, k, oid in self.grid.get((gi + di, gj + dj), []):
                    d = haversine_m(lat, lon, la, lo)
                    if d <= RULE_RADII[k]:
                        hits.append((k, round(d, 1), oid))
        return sorted(hits, key=lambda h: h[1])

    def road_profile(self, street):
        """(two_way, multi_lane_my_side, highway class) for the street name; unknown -> conservative guess."""
        if not street or street not in self.oneway:
            return True, True, None
        oneway = self.oneway[street]
        lanes = self.lanes.get(street)
        hw = self.highway.get(street)
        if lanes is not None:
            multi = lanes >= (2 if oneway else 3)
        else:
            multi = hw in ("trunk", "primary", "secondary")
        return (not oneway), multi, hw


# ---------------- geometry ----------------
def calibrate(rows):
    """Fit rear-view car box width vs bottom row: w = a * (y - y_h). Returns (a, y_h, n)."""
    import statistics as st
    W, Y = [], []
    for r in rows[::3]:
        p = det_path(r["segment_source"])
        if not p.exists():
            continue
        d = json.loads(p.read_text())
        for fr in d["frames"][::30]:
            for b in fr["detections"]:
                if b["label"] != "car" or b["confidence"] < 0.6:
                    continue
                x1, y1, x2, y2 = b["bbox"]
                w, h = x2 - x1, y2 - y1
                if h > 0 and abs((x1 + x2) / 2 - CX) < 250 and 1.0 < w / h < 1.7 and y2 < 1000:
                    W.append(w); Y.append(y2)
    if len(W) < 30:
        return 1.4, 740.0, len(W)
    fit = st.linear_regression(Y, W)
    a = fit.slope
    y_h = -fit.intercept / a
    if not (0.8 < a < 2.2 and 600 < y_h < 850):
        return 1.4, 740.0, len(W)
    return a, y_h, len(W)


def keyframes(rows):
    """Distance gate. Yields (row, t_in_segment, gate_reason)."""
    acc = 0.0
    out = []
    for r in rows:
        dist = fnum(r["dist_m"])
        valid = fnum(r["gps_valid_frac"], 0.0) >= 0.5
        if dist is None or not valid:
            for t in (0.5, 1.5, 2.5, 3.5, 4.5):
                out.append((r, t, "no speed: 1 per second"))
            acc = 0.0
            continue
        if dist <= 0:
            continue
        start = acc
        acc += dist
        k = math.floor(start / GATE_M) + 1
        while k * GATE_M <= acc:
            t = (k * GATE_M - start) / dist * 5.0
            out.append((r, min(4.95, max(0.0, t)), f"every {GATE_M:g} m of travel"))
            k += 1
    return out


def frame_at(det, t):
    frames = det.get("frames") or []
    if not frames:
        return None
    return min(frames, key=lambda f: abs(f["time_sec"] - t))


def lateral_m(x, y, a, y_h):
    return (x - CX) * CAR_W_M / (a * (y - y_h))


def forward_m(y, a, y_h):
    h_cam = CAR_W_M / a
    return min(MAX_Z_M, FOCAL_PX * h_cam / max(1.0, (y - y_h)))


def classify_boxes(boxes, a, y_h, two_way, multi):
    """Lane check and kerb rows. Returns list of dicts with side and role (traffic|parked|lane_unclear)."""
    out = []
    for b in boxes:
        x1, y1, x2, y2 = b["bbox"]
        if y2 <= y_h + 25 or (y2 - y1) < 30:
            continue
        side = "L" if (x1 + x2) / 2 < CX else "R"
        bw = EGO_HALF_M * a * (y2 - y_h) / CAR_W_M
        ov = max(0.0, min(x2, CX + bw) - max(x1, CX - bw)) / (2 * bw)
        xin, xout = (x2, x1) if side == "L" else (x1, x2)
        lin, lout = abs(lateral_m(xin, y2, a, y_h)), abs(lateral_m(xout, y2, a, y_h))
        if (side == "L" and xin > CX) or (side == "R" and xin < CX):
            lin = 0.0
        out.append({"bbox": [int(v) for v in b["bbox"]], "label": b["label"], "conf": round(b["confidence"], 3),
                    "side": side, "ego_overlap": round(ov, 2), "lat_in_m": round(lin, 2), "lat_out_m": round(lout, 2)})
    for side in ("L", "R"):
        cand = [o for o in out if o["side"] == side and o["ego_overlap"] < EGO_OVERLAP]
        for o in out:
            if o["side"] == side and o["ego_overlap"] >= EGO_OVERLAP:
                o["role"], o["why"] = "traffic", f"covers {o['ego_overlap']:.0%} of the ego lane band"
        if not cand:
            continue
        kerb_out = max(o["lat_out_m"] for o in cand)
        # minimum lateral gap from our path to a kerb row: one lane of other traffic between us and the kerb?
        if side == "L" and two_way:
            need, why = 5.0, "left of a two-way road: oncoming lane between us and the kerb"
        elif multi:
            need, why = 4.5, "multi-lane road: car could be in the next lane"
        else:
            need, why = 1.8, "single lane our side"
        for o in cand:
            if o["lat_out_m"] >= kerb_out - 2.0 and o["lat_in_m"] >= need:
                o["role"], o["why"] = "parked", f"outermost row, inner edge {o['lat_in_m']} m from our path ({why})"
            else:
                o["role"], o["why"] = "lane_unclear", f"inner edge {o['lat_in_m']} m from our path, needs >= {need} m ({why})"
    return out


def gaps_for_side(objs, side):
    """Gaps between consecutive parked boxes on one side, ordered outward from the image centre."""
    parked = sorted([o for o in objs if o["side"] == side and o["role"] == "parked"],
                    key=lambda o: o["bbox"][0])
    # merge boxes overlapping in x into occupied runs
    runs = []
    for o in parked:
        x1, _, x2, _ = o["bbox"]
        if runs and x1 <= runs[-1]["x2"]:
            runs[-1]["x2"] = max(runs[-1]["x2"], x2); runs[-1]["boxes"].append(o)
        else:
            runs.append({"x1": x1, "x2": x2, "boxes": [o]})
    gaps = []
    for A, B in zip(runs, runs[1:]):
        g = B["x1"] - A["x2"]
        a_box = max(A["boxes"], key=lambda o: o["bbox"][2])   # box bounding the gap on the left
        b_box = min(B["boxes"], key=lambda o: o["bbox"][0])   # box bounding the gap on the right
        wa = a_box["bbox"][2] - a_box["bbox"][0]
        wb = b_box["bbox"][2] - b_box["bbox"][0]
        ratio = g / math.sqrt(max(1, wa) * max(1, wb))
        y1 = min(a_box["bbox"][1], b_box["bbox"][1])
        y2 = max(a_box["bbox"][3], b_box["bbox"][3])
        gaps.append({"box": [A["x2"], y1, B["x1"], y2], "gap_px": g, "ratio": round(ratio, 2),
                     "neighbours": [a_box, b_box]})
    return gaps


def slots_for_drive(set_name):
    t0 = time.time()
    rows = load_segments(set_name)
    osm = OSM(set_name)
    a, y_h, ncal = calibrate(rows)
    kfs = keyframes(rows)
    seen_seg = set()
    total_frames = 0
    for r in rows:
        p = det_path(r["segment_source"])
        if p.exists():
            dd = json.loads(p.read_text())
            total_frames += int(dd.get("frame_count") or len(dd.get("frames", [])))
    slots = []
    lane_unclear_boxes = 0
    for idx, (r, t, gate_why) in enumerate(kfs):
        src = r["segment_source"]
        p = det_path(src)
        if not p.exists():
            continue
        det = json.loads(p.read_text())
        if src not in seen_seg:
            seen_seg.add(src)
        fr = frame_at(det, t)
        if fr is None:
            continue
        boxes = [b for b in fr["detections"] if b["label"] in VEHICLES and b["confidence"] >= MIN_CONF]
        two_way, multi, hw = osm.road_profile(r["street_main"])
        objs = classify_boxes(boxes, a, y_h, two_way, multi)
        kf = {"set": set_name, "kf_index": idx, "segment_source": src, "chunk": int(r["chunk"]),
              "segment": int(r["segment"]), "t": round(t, 3), "camera_id": r["camera_id"],
              "original_video": r["original_video"], "segment_start_sec": r["segment_start_sec"], "frame_index": fr["frame_index"],
              "frame_time": fr["time_sec"], "gate": gate_why, "street": r["street_main"],
              "road": {"two_way": two_way, "multi_lane_our_side": multi, "highway": hw},
              "n_vehicle_boxes": len(boxes)}
        base = f"dc_{set_name}_{int(r['chunk']):04d}_{int(r['segment'])}_{int(round(t * 10)):02d}"
        n = 0
        for side in ("L", "R"):
            side_objs = [o for o in objs if o["side"] == side]
            for o in side_objs:
                if o["role"] == "parked":
                    n += 1
                    slots.append({**kf, "slot_id": f"{base}_{side}{n:02d}", "side": side, "kind": "car", "box": o["bbox"],
                                  "objs": side_objs, "state": "occupied", "reason": "parked car in kerb row: " + o["why"],
                                  "confidence": o["conf"], "decided_by": "geometry", "rule_hits": []})
                elif o["role"] == "lane_unclear":
                    n += 1
                    lane_unclear_boxes += 1
                    slots.append({**kf, "slot_id": f"{base}_{side}{n:02d}", "side": side, "kind": "unclear_car",
                                  "box": o["bbox"], "objs": side_objs, "state": "cannot_tell",
                                  "reason": "lane unclear: " + o["why"], "confidence": None,
                                  "decided_by": "geometry", "rule_hits": []})
            for g in gaps_for_side(objs, side):
                if g["ratio"] < GAP_SHORT_RATIO:
                    continue
                n += 1
                s = {**kf, "slot_id": f"{base}_{side}{n:02d}", "side": side, "kind": "gap", "box": g["box"],
                     "gap_px": g["gap_px"], "ratio": g["ratio"], "neighbours": [nb["bbox"] for nb in g["neighbours"]],
                     "neighbour_conf": [nb["conf"] for nb in g["neighbours"]], "objs": side_objs, "rule_hits": [],
                     "decided_by": "geometry", "confidence": None}
                gx1, gy1, gx2, gy2 = g["box"]
                blockers = [o for o in objs if o["role"] in ("traffic", "lane_unclear")
                            and min(gx2, o["bbox"][2]) - max(gx1, o["bbox"][0]) > 0.3 * max(1, gx2 - gx1)
                            ]
                if g["ratio"] < GAP_MIN_RATIO:
                    s.update(state="not_space", reason=f"gap {g['ratio']}x a parked-car width, shorter than one car length",
                             confidence=0.6)
                elif any(o["role"] == "lane_unclear" for o in blockers):
                    s.update(state="cannot_tell", reason="lane unclear: a car of unclear lane sits in front of this gap")
                elif blockers:
                    s.update(state="cannot_tell", reason="kerb hidden by traffic in our lane")
                else:
                    s.update(state="candidate", reason="gap >= one car length between parked cars")
                slots.append(s)
    # position + rules
    for s in slots:
        r_pos = position(s, rows_by_src(rows), a, y_h)
        s.update(r_pos)
        if s["kind"] == "gap" and s["state"] == "candidate":
            hits = osm.near(s["lat"], s["lon"]) if osm.ok else []
            s["rule_hits"] = hits
            if not osm.ok:
                s.update(state="cannot_tell", reason="OSM rules unavailable for this drive", decided_by="rule")
            elif hits:
                k, d, oid = hits[0]
                s.update(state="not_space", reason=f"near {k.replace('_', ' ')} ({d} m, OSM node {oid})",
                         confidence=0.7, decided_by="rule")
            elif s["position_status"] != "ok":
                s.update(state="cannot_tell", reason="position uncertain, rules cannot be checked", decided_by="rule")
            elif (GAP_MIN_RATIO <= s["ratio"] <= GAP_CLEAR_MAX and min(s["neighbour_conf"]) >= 0.6
                  and s["gap_px"] >= 40):
                s.update(state="free", reason=f"gap {s['ratio']}x a parked-car width between two parked cars, "
                                               f"no junction/crossing/hydrant/bus stop/driveway in OSM nearby",
                         confidence=round(min(s["neighbour_conf"]) * 0.8, 2), decided_by="rule")
            else:
                why = []
                if s["ratio"] > GAP_CLEAR_MAX:
                    why.append(f"long gap ({s['ratio']}x): driveway or no-stopping zone?")
                if min(s["neighbour_conf"]) < 0.6:
                    why.append("weak neighbour box")
                if s["gap_px"] < 40:
                    why.append("gap under 40 px")
                s.update(state="unclear", reason="; ".join(why) or "unclear")
    # Cosmos queue within the share budget
    unclear = [s for s in slots if s["state"] == "unclear"]
    budget = int(COSMOS_SHARE * len(slots))
    unclear.sort(key=lambda s: -s["gap_px"])
    for i, s in enumerate(unclear):
        s["cosmos_queued"] = i < budget
    stats = {"set": set_name, "segments": sum(1 for r in rows if det_path(r["segment_source"]).exists()), "segments_in_drive": len(rows), "keyframes": len(kfs),
             "segments_with_keyframes": len(seen_seg), "frames_in_drive": total_frames, "gate_share": round(len(kfs) / max(1, total_frames), 4),
             "calibration": {"a": round(a, 3), "y_h": round(y_h, 1), "n_boxes": ncal}, "slots": len(slots),
             "lane_unclear_cars": lane_unclear_boxes,
             "lane_unclear_gaps": sum(1 for s in slots if s["kind"] == "gap" and s["reason"].startswith("lane unclear")),
             "unclear_gaps": len(unclear), "cosmos_budget": budget, "osm_loaded": osm.ok,
             "seconds_slots_stage": round(time.time() - t0, 1)}
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / f"{set_name}_slots.json").write_text(json.dumps({"stats": stats, "slots": slots}))
    print(json.dumps(stats))
    print(collections.Counter((s["kind"], s["state"]) for s in slots))
    return stats


_RBS = {}


def rows_by_src(rows):
    key = id(rows)
    if key not in _RBS:
        _RBS[key] = {r["segment_source"]: r for r in rows}
    return _RBS[key]


def position(s, rbs, a, y_h):
    r = rbs[s["segment_source"]]
    la0, lo0, la1, lo1 = (fnum(r[k]) for k in ("lat_start", "lon_start", "lat_end", "lon_end"))
    valid = fnum(r["gps_valid_frac"], 0.0) >= 0.5 and None not in (la0, lo0, la1, lo1)
    if None in (la0, lo0):
        return {"lat": None, "lon": None, "position_status": "uncertain", "pos_detail": {"why": "no GPS"}}
    if la1 is None:
        la1, lo1 = la0, lo0
    f = s["t"] / 5.0
    lat = la0 + (la1 - la0) * f
    lon = lo0 + (lo1 - lo0) * f
    hd = fnum(r["heading_mean_deg"])
    x1, y1, x2, y2 = s["box"]
    xm, ym = (x1 + x2) / 2, y2
    if hd is None or ym <= y_h + 5:
        return {"lat": lat, "lon": lon, "position_status": "uncertain",
                "pos_detail": {"why": "no heading or point above horizon", "gps_t": s["t"]}}
    X = lateral_m(xm, ym, a, y_h)
    Z = forward_m(ym, a, y_h)
    th = math.radians(hd)
    north = Z * math.cos(th) - X * math.sin(th)
    east = Z * math.sin(th) + X * math.cos(th)
    lat2 = lat + north / 111320.0
    lon2 = lon + east / (111320.0 * math.cos(math.radians(lat)))
    return {"lat": round(lat2, 7), "lon": round(lon2, 7), "position_status": "ok" if valid else "uncertain",
            "pos_detail": {"gps_lat": round(lat, 7), "gps_lon": round(lon, 7), "heading": hd,
                           "forward_m": round(Z, 1), "side_m": round(X, 1),
                           "note": "GPS interpolated at keyframe time; forward distance assumes focal 1000 px"}}


# ---------------- Cosmos ----------------
PROMPT = ("Dashcam frame from a car driving in Toronto. The yellow box marks one stretch of the road-side kerb between "
          "two parked cars. Is that stretch a free, legal on-street parking space for a car? Answer with exactly one "
          "word: FREE, OCCUPIED, NOT_A_SPACE or CANNOT_TELL.")
WORDS = {"FREE": "free", "OCCUPIED": "occupied", "NOT_A_SPACE": "not_space", "CANNOT_TELL": "cannot_tell"}


def cosmos_up():
    import requests
    try:
        return requests.get(COSMOS_URL + "/health", timeout=5).status_code == 200
    except Exception:
        return False


def ask_cosmos(jpg):
    import requests
    img = base64.b64encode(jpg.read_bytes()).decode()
    t = time.time()
    r = requests.post(COSMOS_URL + "/v1/chat/completions", timeout=180, json={
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img}},
            {"type": "text", "text": PROMPT}]}],
        "max_tokens": 8, "temperature": 0, "logprobs": True, "top_logprobs": 5})
    r.raise_for_status()
    j = r.json()
    text = j["choices"][0]["message"]["content"].strip().upper()
    word = next((w for w in WORDS if text.startswith(w)), None)
    if word is None:
        word = next((w for w in WORDS if w.startswith(text[:3]) and text[:3]), None)
    p = None
    try:
        p = math.exp(j["choices"][0]["logprobs"]["content"][0]["logprob"])
    except Exception:
        pass
    return {"raw": text, "word": word, "p_first_token": None if p is None else round(p, 3),
            "seconds": round(time.time() - t, 2)}


def frame_key(s):
    return s["slot_id"]


def cosmos_requests(set_name):
    d = json.loads((WORK / f"{set_name}_slots.json").read_text())
    reqs = [{"key": frame_key(s), "source": s["segment_source"], "t": s["frame_time"], "box": s["box"]}
            for s in d["slots"] if s.get("cosmos_queued")]
    (WORK / f"{set_name}_frame_requests.json").write_text(json.dumps(reqs))
    print(len(reqs), "frame requests")
    return reqs


def run_cosmos(set_name):
    path = WORK / f"{set_name}_slots.json"
    d = json.loads(path.read_text())
    up = cosmos_up()
    calls = []
    for s in d["slots"]:
        if s["state"] != "unclear":
            continue
        if not s.get("cosmos_queued"):
            s.update(state="cannot_tell", reason=s["reason"] + "; not sent to model (5% budget)", decided_by="rule")
            continue
        jpg = FRAME_DIR / f"{frame_key(s)}.jpg"
        if not up:
            s.update(state="cannot_tell", reason="model unavailable", decided_by="cosmos"); continue
        if not jpg.exists():
            s.update(state="cannot_tell", reason="frame unavailable for model check", decided_by="cosmos"); continue
        try:
            ans = ask_cosmos(jpg)
        except Exception as exc:
            s.update(state="cannot_tell", reason=f"model unavailable ({type(exc).__name__})", decided_by="cosmos"); continue
        calls.append(ans["seconds"])
        s["cosmos"] = ans
        p = ans["p_first_token"]
        if ans["word"] is None:
            s.update(state="cannot_tell", reason=f"model answer not one of the four words ({ans['raw'][:20]})")
        elif p is not None and p < COSMOS_MIN_P:
            s.update(state="cannot_tell", reason=f"model said {ans['word']} with low confidence ({p})")
        else:
            s.update(state=WORDS[ans["word"]], reason=f"model check on boxed gap: {ans['word']} (p={p}); "
                                                       f"rules could not settle: {s['reason']}")
        s.update(decided_by="cosmos", confidence=p)
    d["stats"].update(cosmos_up=up, cosmos_calls=len(calls),
                      cosmos_seconds_mean=round(sum(calls) / len(calls), 2) if calls else None,
                      cosmos_share=round(len(calls) / max(1, d["stats"]["slots"]), 4))
    path.write_text(json.dumps(d))
    print(json.dumps({k: d["stats"][k] for k in ("cosmos_up", "cosmos_calls", "cosmos_seconds_mean", "cosmos_share")}))


# ---------------- write ----------------
def dedupe(slots):
    """Merge sightings of the same kerb stretch across keyframes (same side and kind, within 3.5 m)."""
    from common.kerbdb import haversine_m
    spaces = []
    for s in slots:
        if s["lat"] is None:
            spaces.append([s]); continue
        kind = "gap" if s["kind"] == "gap" else "car"
        best = None
        for sp in spaces[-40:]:
            h = sp[-1]
            if h["side"] != s["side"] or ("gap" if h["kind"] == "gap" else "car") != kind or h["lat"] is None:
                continue
            if s["kf_index"] - h["kf_index"] > 6 or s["kf_index"] == h["kf_index"]:
                continue
            d = haversine_m(h["lat"], h["lon"], s["lat"], s["lon"])
            if d <= 3.5 and (best is None or d < best[0]):
                best = (d, sp)
        if best:
            best[1].append(s)
        else:
            spaces.append([s])
    return spaces


def receipt_text(s, n_obs):
    st = {"free": "FREE space", "occupied": "OCCUPIED (parked car)", "not_space": "NOT A SPACE",
          "cannot_tell": "CANNOT TELL"}[s["state"]]
    side = "left" if s["side"] == "L" else "right"
    pos = (f"{s['lat']:.6f}, {s['lon']:.6f}" if s["position_status"] == "ok" else "position uncertain")
    return (f"{st} on the {side} kerb of {s['street'] or 'unnamed road'} ({pos}). Seen in {s['set']} dashcam segment "
            f"{s['segment_source'].rsplit('/', 1)[-1]} at {s['t']:.1f}-{min(5.0, s['t'] + 1):.1f} s "
            f"(frame {s['frame_index']}). Why: {s['reason']}. Decided by {s['decided_by']}; "
            f"{n_obs} sighting(s).")


def write_drive(set_name):
    os.environ.setdefault("KERB_DB", "local")
    os.environ.setdefault("KERB_SQLITE", r"C:\Users\samar\bystander-data\local-lab\kerb.sqlite")
    from common import kerbdb
    d = json.loads((WORK / f"{set_name}_slots.json").read_text())
    slots = [s for s in d["slots"] if s["state"] in ("free", "occupied", "not_space", "cannot_tell")]
    bad = [s for s in d["slots"] if s["state"] not in ("free", "occupied", "not_space", "cannot_tell")]
    if bad:
        raise SystemExit(f"{len(bad)} slots still undecided; run the cosmos stage first")
    spaces = dedupe(slots)
    db = kerbdb.get_db("local")
    db.ensure_tables()
    with db._lock, db._conn:   # re-runs replace this drive's own rows only
        for t, k in (("kerb_spaces", "space_id"), ("kerb_observations", "obs_id"), ("kerb_trail", "trail_id")):
            db._conn.execute(f"DELETE FROM {t} WHERE {k} LIKE ?", (f"dc_{set_name}_%",))
    at = now()
    sp_rows, obs_rows, trail_rows = [], [], []
    for group in spaces:
        best = min(group, key=lambda s: s.get("pos_detail", {}).get("forward_m", 99))   # closest view decides
        sid = group[0]["slot_id"]
        last = group[-1]
        start = round(group[0]["segment_start_sec"] + group[0]["t"], 2)
        conf = best["confidence"]
        length = round(best["ratio"] * CAR_LEN_M, 1) if best["kind"] == "gap" else None
        sp_rows.append({
            "space_id": sid, "source": "dashcam", "camera_id": best["camera_id"] if "camera_id" in best else "pie_cam-3",
            "original_video": best.get("original_video") or None, "segment_number": best["segment"],
            "t_sec": float(best["t"]), "side": best["side"], "state": best["state"], "reason": best["reason"],
            "confidence": None if conf is None else float(conf), "lat": best["lat"], "lon": best["lon"],
            "position_status": best["position_status"], "street": best["street"] or None, "length_m": length,
            "last_seen": at, "evidence_uri": best["segment_source"], "receipt_text": receipt_text(best, len(group)),
            "embedding": None})
        for i, s in enumerate(group):
            obs_rows.append({"obs_id": f"{sid}_o{i}", "space_id": sid, "source": "dashcam", "camera_id": "pie_cam-3",
                             "segment_source": s["segment_source"], "t_sec": float(s["t"]), "state": s["state"],
                             "confidence": None if s["confidence"] is None else float(s["confidence"])})
        steps = [
            ("gate", {"keyframe": best["kf_index"], "segment_source": best["segment_source"], "t": best["t"],
                      "frame_index": best["frame_index"], "rule": best["gate"], "drive_gate_share": d["stats"]["gate_share"],
                      "evidence_start_s": best["t"], "evidence_end_s": min(5.0, best["t"] + 1.0)}),
            ("boxes", {"n_vehicle_boxes": best["n_vehicle_boxes"], "min_conf": MIN_CONF, "classes": sorted(VEHICLES),
                       "side_boxes": best["objs"]}),
            ("geometry", {"kind": best["kind"], "box": best["box"], "ratio": best.get("ratio"),
                          "gap_px": best.get("gap_px"), "neighbours": best.get("neighbours"), "road": best["road"],
                          "calibration": d["stats"]["calibration"], "result": best["state"] if best["decided_by"] == "geometry" else "candidate"}),
            ("rule", {"hits": best["rule_hits"], "radii_m": RULE_RADII, "decided": best["decided_by"] == "rule",
                      "state": best["state"]}),
        ]
        if best.get("cosmos") or best["decided_by"] == "cosmos":
            steps.append(("cosmos", {"sent": bool(best.get("cosmos")), **(best.get("cosmos") or {}),
                                     "min_p": COSMOS_MIN_P, "state": best["state"], "reason": best["reason"]}))
        steps.append(("position", {**best.get("pos_detail", {}), "lat": best["lat"], "lon": best["lon"],
                                   "status": best["position_status"], "sightings": len(group)}))
        for n, (step, det) in enumerate(steps, 1):
            trail_rows.append({"trail_id": f"{sid}_{n}", "space_id": sid, "step_no": n, "step": step,
                               "detail_json": json.dumps(det), "at": at})
    db.insert("kerb_trail", trail_rows)
    db.insert("kerb_observations", obs_rows)
    for r in sp_rows:
        db.upsert_space(r)
    OUT.mkdir(parents=True, exist_ok=True)
    for name, rows in (("spaces", sp_rows), ("observations", obs_rows), ("trail", trail_rows)):
        with open(OUT / f"{set_name}_kerb_{name}.jsonl", "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    by_state = collections.Counter(r["state"] for r in sp_rows)
    d["stats"].update(receipts=len(sp_rows), receipts_by_state=dict(by_state), observations=len(obs_rows),
                      trail_rows=len(trail_rows))
    (WORK / f"{set_name}_slots.json").write_text(json.dumps(d))
    (WORK / f"{set_name}_stats.json").write_text(json.dumps(d["stats"], indent=1))
    print(json.dumps(d["stats"]))


if __name__ == "__main__":
    stage, sets = sys.argv[1], sys.argv[2:]
    for s in sets:
        {"slots": slots_for_drive, "requests": cosmos_requests, "cosmos": run_cosmos, "write": write_drive}[stage](s)
