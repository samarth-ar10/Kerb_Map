"""Kerb pipeline for one uploaded dashcam clip (flat module, stdlib only; deployable in the ConfigMap).

The steps and constants are taken from dashcam/receipts.py (the PIE batch pipeline):
  gate      keyframe every ~8 m of travel (from the GPS track), else 1 per second without GPS
  boxes     stored YOLO boxes of that frame (car/truck/bus, conf >= 0.4), read from VSS detections
  geometry  left/right kerb rows, lane check (lane unclear -> cannot_tell), gaps >= one car length
  rule      OSM junctions, driveways, crossings, signals, hydrants, bus stops (skipped if no map data)
  cosmos    only for gaps the rules cannot settle, via COSMOS3_REASON_URL; else cannot_tell "model unavailable"
  position  GPS interpolated at the keyframe time + forward/side offset; else "position uncertain"

Inputs are plain data, so the same code runs on cached detections in tests:
  segments = [{"source", "segment_number", "start_sec", "det": <detections body>}]
  track    = parse_gps_csv(text) (t_sec,lat,lon,heading_deg,speed_kmh) or None
"""

import base64
import collections
import csv
import io
import json
import math
import os
import statistics
import urllib.parse
import urllib.request

from common.kerbdb import haversine_m

GATE_M = 8.0
VEHICLES = {"car", "truck", "bus"}
MIN_CONF = 0.4
REF_W = 1920.0               # boxes are rescaled to a 1920 px wide frame (the constants below assume it)
CX = REF_W / 2
CAR_W_M = 1.8
FOCAL_PX = 1000.0
MAX_Z_M = 40.0
EGO_HALF_M = 1.5
EGO_OVERLAP = 0.3
GAP_MIN_RATIO = 1.1
GAP_SHORT_RATIO = 0.6
GAP_CLEAR_MAX = 3.0
CAR_LEN_M = 5.0
COSMOS_MAX_CALLS = 3         # per clip; the GPU is shared
COSMOS_MIN_P = 0.85
RULE_RADII = {"junction": 12.0, "driveway": 6.0, "crossing": 10.0, "traffic_signals": 15.0,
              "fire_hydrant": 5.0, "bus_stop": 15.0, "stop": 10.0}
ROAD = {"motorway", "trunk", "primary", "secondary", "tertiary", "residential", "unclassified", "living_street",
        "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}
STATES = ("free", "occupied", "not_space", "cannot_tell")


def fnum(v, default=None):
    try:
        return float(v) if v not in ("", None) else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- GPS track

def parse_gps_csv(text):
    """CSV with header t_sec,lat,lon,heading_deg,speed_kmh (heading/speed optional). Sorted by t."""
    out = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        t, la, lo = fnum(r.get("t_sec")), fnum(r.get("lat")), fnum(r.get("lon"))
        if None in (t, la, lo):
            continue
        out.append({"t": t, "lat": la, "lon": lo, "heading": fnum(r.get("heading_deg")),
                    "speed": fnum(r.get("speed_kmh"))})
    out.sort(key=lambda p: p["t"])
    return out


def track_from_rows(rows):
    """[[t, lat, lon, heading, speed], ...] (samples_gps.json) -> track."""
    return [{"t": float(r[0]), "lat": float(r[1]), "lon": float(r[2]),
             "heading": None if r[3] is None else float(r[3]), "speed": None if r[4] is None else float(r[4])}
            for r in rows]


def _bearing(a, b):
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dl = math.radians(b["lon"] - a["lon"])
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def gps_at(track, t):
    """(lat, lon, heading) at clip time t, interpolated; None outside the track (> 1 s beyond its ends)."""
    if not track or t < track[0]["t"] - 1.0 or t > track[-1]["t"] + 1.0:
        return None
    if len(track) == 1:
        p = track[0]
        return p["lat"], p["lon"], p["heading"]
    for a, b in zip(track, track[1:]):
        if t <= b["t"] or b is track[-1]:
            span = b["t"] - a["t"]
            f = 0.0 if span <= 0 else min(1.0, max(0.0, (t - a["t"]) / span))
            lat = a["lat"] + (b["lat"] - a["lat"]) * f
            lon = a["lon"] + (b["lon"] - a["lon"]) * f
            hd = a["heading"] if a["heading"] is not None else b["heading"]
            if haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) > 1.0:
                hd = _bearing(a, b) if hd is None else hd
            return lat, lon, hd
    return None


def _dist_fn(track):
    cum = [0.0]
    for a, b in zip(track, track[1:]):
        cum.append(cum[-1] + haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]))

    def dist(t):
        if t <= track[0]["t"]:
            return 0.0
        for i in range(1, len(track)):
            if t <= track[i]["t"]:
                span = track[i]["t"] - track[i - 1]["t"]
                f = 0.0 if span <= 0 else (t - track[i - 1]["t"]) / span
                return cum[i - 1] + (cum[i] - cum[i - 1]) * f
        return cum[-1]
    return dist


def keyframe_times(segments, track):
    """[(segment, local_t, why)]: every GATE_M metres of travel with GPS; 1 per second without.

    A segment where the car does not move GATE_M gets one keyframe at its middle (stationary)."""
    out = []
    dist = _dist_fn(track) if track and len(track) >= 2 else None
    for seg in segments:
        dur = _duration(seg)
        picked = []
        if dist is None:
            picked = [(t + 0.5, "no GPS: 1 per second") for t in range(int(math.ceil(dur)))
                      if t + 0.5 < dur]
        else:
            step = 0.1
            t = 0.0
            last = math.floor(dist(seg["start_sec"]) / GATE_M)
            while t < dur:
                k = math.floor(dist(seg["start_sec"] + t) / GATE_M)
                if k > last:
                    picked.append((round(t, 2), f"every {GATE_M:g} m of travel"))
                    last = k
                t += step
            if not picked:
                picked = [(round(dur / 2, 2), f"moved under {GATE_M:g} m: 1 per segment")]
        out += [(seg, t, why) for t, why in picked]
    return out


def _duration(seg):
    frames = (seg.get("det") or {}).get("frames") or []
    if frames:
        return max(f.get("time_sec", 0.0) for f in frames) + 1.0 / max(1.0, fnum(seg["det"].get("fps"), 30.0))
    return 5.0


# ---------------------------------------------------------------- OSM (optional)

OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
OVERPASS_QUERY = """[out:json][timeout:20];
(
  way["highway"]({b});
  node["highway"~"^(traffic_signals|crossing|stop|bus_stop|give_way)$"]({b});
  node["crossing"]({b});
  node["emergency"="fire_hydrant"]({b});
  node["public_transport"~"^(platform|stop_position)$"]({b});
  node["railway"="tram_stop"]({b});
);
out body;
>;
out skel qt;"""


def fetch_osm(track, timeout=12):
    """Overpass elements around the track, or (None, why). KERB_OSM_JSON: a cached Overpass file instead;
    KERB_OSM=off skips the network."""
    cached = os.environ.get("KERB_OSM_JSON")
    if cached and os.path.isfile(cached):
        with open(cached, encoding="utf-8") as fh:
            return json.load(fh).get("elements", []), f"cached map file {os.path.basename(cached)}"
    if not track:
        return None, "no GPS, map rules cannot be checked"
    if os.environ.get("KERB_OSM", "").lower() in ("off", "0", "no"):
        return None, "map data switched off"
    m = 0.0008
    b = (min(p["lat"] for p in track) - m, min(p["lon"] for p in track) - m,
         max(p["lat"] for p in track) + m, max(p["lon"] for p in track) + m)
    q = OVERPASS_QUERY.format(b=",".join(f"{x:.6f}" for x in b))
    errors = []
    for url in OVERPASS:
        try:
            req = urllib.request.Request(url, data=("data=" + urllib.parse.quote(q)).encode(),
                                         headers={"User-Agent": "KerbMap-hackathon/0.1 (team 5)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r).get("elements", []), "OpenStreetMap (Overpass)"
        except Exception as exc:
            errors.append(type(exc).__name__)
    return None, "map data unavailable (" + ", ".join(errors) + ")"


class OSM:
    def __init__(self, elements):
        self.ok = elements is not None
        self.points, self.road_nodes = [], []
        self.oneway, self.lanes, self.highway = {}, {}, {}
        self.grid = collections.defaultdict(list)
        if not self.ok:
            return
        nodes = {e["id"]: e for e in elements if e["type"] == "node"}
        use = collections.defaultdict(set)
        votes = collections.defaultdict(collections.Counter)
        for e in elements:
            if e["type"] != "way":
                continue
            t = e.get("tags", {})
            hw = t.get("highway", "")
            kind = "road" if hw in ROAD else ("service" if hw == "service" else None)
            if not kind:
                continue
            name = t.get("name")
            for n in e.get("nodes", []):
                use[n].add((kind, e["id"]))
                if kind == "road" and name and n in nodes and "lat" in nodes[n]:
                    self.road_nodes.append((nodes[n]["lat"], nodes[n]["lon"], name))
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
            if len(roads) >= 2:
                self.points.append((n["lat"], n["lon"], "junction", nid))
            elif roads and any(k == "service" for k, _ in ways):
                self.points.append((n["lat"], n["lon"], "driveway", nid))
        for e in elements:
            if e["type"] != "node" or "tags" not in e:
                continue
            t = e["tags"]
            kinds = set()
            hw = t.get("highway")
            if hw in ("traffic_signals", "stop"):
                kinds.add(hw)
            if hw == "crossing" or "crossing" in t:
                kinds.add("crossing")
            if hw == "bus_stop" or t.get("public_transport") in ("platform", "stop_position") \
                    or t.get("railway") == "tram_stop":
                kinds.add("bus_stop")
            if t.get("emergency") == "fire_hydrant":
                kinds.add("fire_hydrant")
            for k in kinds:
                self.points.append((e["lat"], e["lon"], k, e["id"]))
        for p in self.points:
            self.grid[(int(p[0] / 0.0005), int(p[1] / 0.0005))].append(p)

    def near(self, lat, lon):
        gi, gj = int(lat / 0.0005), int(lon / 0.0005)
        hits = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for la, lo, k, oid in self.grid.get((gi + di, gj + dj), []):
                    d = haversine_m(lat, lon, la, lo)
                    if d <= RULE_RADII[k]:
                        hits.append((k, round(d, 1), oid))
        return sorted(hits, key=lambda h: h[1])

    def street_at(self, lat, lon, max_m=30.0):
        best = None
        for la, lo, name in self.road_nodes:
            if abs(la - lat) > 0.0005 or abs(lo - lon) > 0.0007:
                continue
            d = haversine_m(lat, lon, la, lo)
            if d <= max_m and (best is None or d < best[0]):
                best = (d, name)
        return best[1] if best else None

    def road_profile(self, street):
        """(two_way, multi_lane_our_side, highway); unknown street -> conservative (two-way, multi-lane)."""
        if not street or street not in self.oneway:
            return True, True, None
        oneway = self.oneway[street]
        lanes = self.lanes.get(street)
        hw = self.highway.get(street)
        multi = lanes >= (2 if oneway else 3) if lanes is not None else hw in ("trunk", "primary", "secondary")
        return (not oneway), multi, hw


# ---------------------------------------------------------------- geometry (from dashcam/receipts.py)

def _scale(det):
    frames = det.get("frames") or []
    shape = (frames[0].get("shape") if frames else None) or det.get("video_shape") or [1080, 1920]
    return REF_W / float(shape[1] or 1920)


def calibrate(segments):
    """Fit rear-view car box width vs bottom row: w = a * (y - y_h). Returns (a, y_h, n)."""
    W, Y = [], []
    for seg in segments:
        det = seg.get("det") or {}
        s = _scale(det)
        for fr in (det.get("frames") or [])[::30]:
            for b in fr["detections"]:
                if b["label"] != "car" or b["confidence"] < 0.6:
                    continue
                x1, y1, x2, y2 = (v * s for v in b["bbox"])
                w, h = x2 - x1, y2 - y1
                if h > 0 and abs((x1 + x2) / 2 - CX) < 250 and 1.0 < w / h < 1.7 and y2 < 1000:
                    W.append(w)
                    Y.append(y2)
    if len(W) < 30:
        return 1.4, 740.0, len(W)
    fit = statistics.linear_regression(Y, W)
    a = fit.slope
    if a == 0:
        return 1.4, 740.0, len(W)
    y_h = -fit.intercept / a
    if not (0.8 < a < 2.2 and 600 < y_h < 850):
        return 1.4, 740.0, len(W)
    return a, y_h, len(W)


def lateral_m(x, y, a, y_h):
    return (x - CX) * CAR_W_M / (a * (y - y_h))


def forward_m(y, a, y_h):
    return min(MAX_Z_M, FOCAL_PX * (CAR_W_M / a) / max(1.0, (y - y_h)))


def classify_boxes(boxes, a, y_h, two_way, multi):
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
    parked = sorted([o for o in objs if o["side"] == side and o.get("role") == "parked"], key=lambda o: o["bbox"][0])
    runs = []
    for o in parked:
        x1, _, x2, _ = o["bbox"]
        if runs and x1 <= runs[-1]["x2"]:
            runs[-1]["x2"] = max(runs[-1]["x2"], x2)
            runs[-1]["boxes"].append(o)
        else:
            runs.append({"x1": x1, "x2": x2, "boxes": [o]})
    gaps = []
    for A, B in zip(runs, runs[1:]):
        g = B["x1"] - A["x2"]
        a_box = max(A["boxes"], key=lambda o: o["bbox"][2])
        b_box = min(B["boxes"], key=lambda o: o["bbox"][0])
        wa = a_box["bbox"][2] - a_box["bbox"][0]
        wb = b_box["bbox"][2] - b_box["bbox"][0]
        ratio = g / math.sqrt(max(1, wa) * max(1, wb))
        y1 = min(a_box["bbox"][1], b_box["bbox"][1])
        y2 = max(a_box["bbox"][3], b_box["bbox"][3])
        gaps.append({"box": [A["x2"], y1, B["x1"], y2], "gap_px": g, "ratio": round(ratio, 2),
                     "neighbours": [a_box, b_box]})
    return gaps


def position(track, t_global, box, a, y_h):
    g = gps_at(track, t_global) if track else None
    if g is None:
        return {"lat": None, "lon": None, "position_status": "uncertain",
                "pos_detail": {"why": "no GPS for this time" if track else "no GPS track uploaded"}}
    lat, lon, hd = g
    x1, y1, x2, y2 = box
    xm, ym = (x1 + x2) / 2, y2
    if hd is None or ym <= y_h + 5:
        return {"lat": round(lat, 7), "lon": round(lon, 7), "position_status": "uncertain",
                "pos_detail": {"why": "no heading or point above horizon", "gps_t": round(t_global, 2)}}
    X, Z = lateral_m(xm, ym, a, y_h), forward_m(ym, a, y_h)
    th = math.radians(hd)
    north = Z * math.cos(th) - X * math.sin(th)
    east = Z * math.sin(th) + X * math.cos(th)
    return {"lat": round(lat + north / 111320.0, 7),
            "lon": round(lon + east / (111320.0 * math.cos(math.radians(lat))), 7),
            "position_status": "ok",
            "pos_detail": {"gps_lat": round(lat, 7), "gps_lon": round(lon, 7), "gps_t": round(t_global, 2),
                           "heading": round(hd, 1), "forward_m": round(Z, 1), "side_m": round(X, 1),
                           "note": "GPS interpolated at keyframe time; forward distance assumes focal 1000 px"}}


# ---------------------------------------------------------------- Cosmos (unclear gaps only)

COSMOS_WORDS = ("NOT_A_SPACE", "CANNOT_TELL", "OCCUPIED", "FREE")
COSMOS_STATE = {"FREE": "free", "OCCUPIED": "occupied", "NOT_A_SPACE": "not_space", "CANNOT_TELL": "cannot_tell"}


def cosmos_settings():
    url = os.environ.get("COSMOS3_REASON_URL") or os.environ.get("KERB_COSMOS_URL")
    return (url.rstrip("/") if url else None), os.environ.get("GPU_BEARER_TOKEN"), \
        os.environ.get("COSMOS3_REASON_MODEL", "nvidia/cosmos3-nano-reasoner")


def ask_cosmos(video_bytes, slot, url, token, model, timeout=30):
    """One question on the 5 s segment video about the gap at slot["t"]; returns (word, p, raw)."""
    side = "left" if slot["side"] == "L" else "right"
    x1, _, x2, _ = slot["box"]
    where = int(round((x1 + x2) / 2 / REF_W * 100))
    prompt = (f"Dashcam clip from a car. At about {slot['t']:.1f} s, look at the {side} kerb, about {where}% across "
              f"the frame from the left, between two parked cars. Is that stretch a free, legal on-street parking "
              f"space for a car? Reply with exactly one word: FREE, OCCUPIED, NOT_A_SPACE or CANNOT_TELL.")
    body = {"model": model, "temperature": 0, "max_tokens": 16, "logprobs": True, "top_logprobs": 5,
            "messages": [{"role": "user", "content": [
                {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," +
                                                    base64.b64encode(video_bytes).decode()}},
                {"type": "text", "text": prompt}]}]}
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.load(r)
    choice = j["choices"][0]
    raw = ((choice.get("message") or {}).get("content") or "").strip().upper()
    word = next((w for w in COSMOS_WORDS if w in raw.replace(" ", "")), None)
    p, built = None, ""
    toks = ((choice.get("logprobs") or {}).get("content")) or []
    if word and toks:
        total = 0.0
        for tk in toks:
            built += tk.get("token") or ""
            total += float(tk.get("logprob") or 0.0)
            if word.replace("_", "") in built.upper().replace(" ", "").replace("_", ""):
                break
        p = round(math.exp(total), 3)
    return word, p, raw[:40]


# ---------------------------------------------------------------- the pipeline

def run(segments, track, fetch_video=None, osm_elements=None, osm_note=None):
    """Slots for one clip. fetch_video(source) -> bytes is used for the Cosmos check (optional).

    Returns {"slots": [...], "stats": {...}}; every slot state is one of STATES."""
    osm = OSM(osm_elements)
    a, y_h, ncal = calibrate(segments)
    kfs = keyframe_times(segments, track)
    slots = []
    for idx, (seg, t, why) in enumerate(kfs):
        det = seg.get("det") or {}
        frames = det.get("frames") or []
        if not frames:
            continue
        fr = min(frames, key=lambda f: abs(f["time_sec"] - t))
        s = _scale(det)
        boxes = [{**b, "bbox": [v * s for v in b["bbox"]]} for b in fr["detections"]
                 if b["label"] in VEHICLES and b["confidence"] >= MIN_CONF]
        tg = seg["start_sec"] + t
        g = gps_at(track, tg) if track else None
        street = osm.street_at(g[0], g[1]) if (g and osm.ok) else None
        two_way, multi, hw = osm.road_profile(street)
        objs = classify_boxes(boxes, a, y_h, two_way, multi)
        kf = {"kf_index": idx, "segment_source": seg["source"], "segment": int(seg["segment_number"]),
              "segment_start_sec": seg["start_sec"], "t": round(t, 2), "t_global": round(tg, 2),
              "frame_index": fr.get("frame_index"), "frame_time": fr.get("time_sec"), "gate": why,
              "street": street, "road": {"two_way": two_way, "multi_lane_our_side": multi, "highway": hw},
              "n_vehicle_boxes": len(boxes), "frame_size": [int(REF_W / s), int(round((fr.get("shape") or [1080])[0]))]}
        n = 0
        for side in ("L", "R"):
            side_objs = [o for o in objs if o["side"] == side]
            for o in side_objs:
                if o.get("role") == "parked":
                    n += 1
                    slots.append({**kf, "n": n, "side": side, "kind": "car", "box": o["bbox"], "objs": side_objs,
                                  "state": "occupied", "reason": "parked car in kerb row: " + o["why"],
                                  "confidence": o["conf"], "decided_by": "geometry", "rule_hits": []})
                elif o.get("role") == "lane_unclear":
                    n += 1
                    slots.append({**kf, "n": n, "side": side, "kind": "unclear_car", "box": o["bbox"],
                                  "objs": side_objs, "state": "cannot_tell", "reason": "lane unclear: " + o["why"],
                                  "confidence": None, "decided_by": "geometry", "rule_hits": []})
            for gp in gaps_for_side(objs, side):
                if gp["ratio"] < GAP_SHORT_RATIO:
                    continue
                n += 1
                sl = {**kf, "n": n, "side": side, "kind": "gap", "box": gp["box"], "gap_px": gp["gap_px"],
                      "ratio": gp["ratio"], "neighbours": [nb["bbox"] for nb in gp["neighbours"]],
                      "neighbour_conf": [nb["conf"] for nb in gp["neighbours"]], "objs": side_objs,
                      "rule_hits": [], "decided_by": "geometry", "confidence": None}
                gx1, _, gx2, _ = gp["box"]
                blockers = [o for o in objs if o.get("role") in ("traffic", "lane_unclear")
                            and min(gx2, o["bbox"][2]) - max(gx1, o["bbox"][0]) > 0.3 * max(1, gx2 - gx1)]
                if gp["ratio"] < GAP_MIN_RATIO:
                    sl.update(state="not_space", confidence=0.6,
                              reason=f"gap {gp['ratio']}x a parked-car width, shorter than one car length")
                elif any(o.get("role") == "lane_unclear" for o in blockers):
                    sl.update(state="cannot_tell", reason="lane unclear: a car of unclear lane sits in front of this gap")
                elif blockers:
                    sl.update(state="cannot_tell", reason="kerb hidden by traffic in our lane")
                else:
                    sl.update(state="candidate", reason="gap >= one car length between parked cars")
                slots.append(sl)

    for sl in slots:
        sl.update(position(track, sl["t_global"], sl["box"], a, y_h))
        if sl["kind"] != "gap" or sl["state"] != "candidate":
            continue
        if sl["position_status"] != "ok":
            sl.update(state="cannot_tell", reason="position uncertain, kerb rules cannot be checked", decided_by="rule")
            continue
        long_or_weak = []
        if sl["ratio"] > GAP_CLEAR_MAX:
            long_or_weak.append(f"long gap ({sl['ratio']}x): driveway or no-stopping zone?")
        if min(sl["neighbour_conf"]) < 0.6:
            long_or_weak.append("weak neighbour box")
        if sl["gap_px"] < 40:
            long_or_weak.append("gap under 40 px")
        if not osm.ok:
            sl.update(state="unclear", reason="; ".join(["kerb rules not checked: " + (osm_note or "no map data")]
                                                        + long_or_weak))
            continue
        hits = osm.near(sl["lat"], sl["lon"])
        sl["rule_hits"] = hits
        if hits:
            k, d, oid = hits[0]
            sl.update(state="not_space", reason=f"near {k.replace('_', ' ')} ({d} m, OSM node {oid})",
                      confidence=0.7, decided_by="rule")
        elif not long_or_weak:
            sl.update(state="free", decided_by="rule", confidence=round(min(sl["neighbour_conf"]) * 0.8, 2),
                      reason=f"gap {sl['ratio']}x a parked-car width between two parked cars, "
                             f"no junction/crossing/hydrant/bus stop/driveway in OSM nearby")
        else:
            sl.update(state="unclear", reason="; ".join(long_or_weak))

    # Cosmos: only the unclear gaps, longest first, at most COSMOS_MAX_CALLS per clip
    url, token, model = cosmos_settings()
    unclear = sorted([s for s in slots if s["state"] == "unclear"], key=lambda s: -s["gap_px"])
    calls, videos = 0, {}
    for i, sl in enumerate(unclear):
        if i >= COSMOS_MAX_CALLS:
            sl.update(state="cannot_tell", decided_by="rule",
                      reason=sl["reason"] + f"; not sent to model (limit {COSMOS_MAX_CALLS} per clip)")
            continue
        if not url or fetch_video is None:
            sl.update(state="cannot_tell", decided_by="cosmos", reason="model unavailable; " + sl["reason"])
            continue
        try:
            if sl["segment_source"] not in videos:
                videos[sl["segment_source"]] = fetch_video(sl["segment_source"])
            word, p, raw = ask_cosmos(videos[sl["segment_source"]], sl, url, token, model)
            calls += 1
        except Exception as exc:
            sl.update(state="cannot_tell", decided_by="cosmos",
                      reason=f"model unavailable ({type(exc).__name__}); " + sl["reason"])
            url = None   # one failure: do not wait on the model again for this clip
            continue
        sl["cosmos"] = {"word": word, "p_first_token": p, "raw": raw, "model": model}
        if word is None:
            sl.update(state="cannot_tell", reason=f"model answer not one of the four words ({raw[:20]})")
        elif p is not None and p < COSMOS_MIN_P:
            sl.update(state="cannot_tell", reason=f"model said {word} with low confidence ({p})")
        else:
            sl.update(state=COSMOS_STATE[word], reason=f"model check on the gap: {word} (p={p}); "
                                                       f"rules could not settle: {sl['reason']}")
        sl.update(decided_by="cosmos", confidence=p)

    stats = {"segments": len(segments), "keyframes": len(kfs), "slots": len(slots),
             "calibration": {"a": round(a, 3), "y_h": round(y_h, 1), "n_boxes": ncal},
             "map": osm_note or ("loaded" if osm.ok else "none"), "osm_loaded": osm.ok,
             "cosmos_calls": calls, "gps_points": len(track or [])}
    return {"slots": slots, "stats": stats}


def dedupe(slots):
    """Merge sightings of the same kerb stretch across keyframes (same side and kind, within 3.5 m)."""
    spaces = []
    for s in slots:
        if s["lat"] is None or s["position_status"] != "ok":
            spaces.append([s])
            continue
        kind = "gap" if s["kind"] == "gap" else "car"
        best = None
        for sp in spaces[-40:]:
            h = sp[-1]
            if h["side"] != s["side"] or ("gap" if h["kind"] == "gap" else "car") != kind or h["lat"] is None \
                    or h["position_status"] != "ok":
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


def receipt_text(s, n_obs, filename):
    st = {"free": "FREE space", "occupied": "OCCUPIED (parked car)", "not_space": "NOT A SPACE",
          "cannot_tell": "CANNOT TELL"}[s["state"]]
    side = "left" if s["side"] == "L" else "right"
    pos = f"{s['lat']:.6f}, {s['lon']:.6f}" if s["position_status"] == "ok" else "position uncertain"
    return (f"{st} on the {side} kerb of {s['street'] or 'unnamed road'} ({pos}). Seen in uploaded clip {filename}, "
            f"segment {s['segment']} at {s['t']:.1f} s (clip time {s['t_global']:.1f} s). Why: {s['reason']}. "
            f"Decided by {s['decided_by']}; {n_obs} sighting(s).")


def build_rows(result, job, at):
    """kerb_spaces / kerb_observations / kerb_trail rows for one job.

    job: {job_id, camera_id, original_video, filename, object_key}. space_id = up_<job_id>_..."""
    jid = job["job_id"]
    groups = dedupe(result["slots"])
    spaces, obs, trail = [], [], []
    for group in groups:
        best = min(group, key=lambda s: s.get("pos_detail", {}).get("forward_m", 99))
        first = group[0]
        sid = f"up_{jid}_{first['segment']:02d}_{int(round(first['t'] * 10)):03d}_{first['side']}{first['n']:02d}"
        length = round(best["ratio"] * CAR_LEN_M, 1) if best["kind"] == "gap" else None
        conf = best["confidence"]
        spaces.append({
            "space_id": sid, "source": "dashcam", "camera_id": job["camera_id"],
            "original_video": job.get("original_video"), "segment_number": best["segment"],
            "t_sec": float(best["t_global"]), "side": best["side"], "state": best["state"], "reason": best["reason"],
            "confidence": None if conf is None else float(conf), "lat": best["lat"], "lon": best["lon"],
            "position_status": best["position_status"], "street": best["street"], "length_m": length,
            "last_seen": at, "evidence_uri": best["segment_source"],
            "receipt_text": receipt_text(best, len(group), job.get("filename") or ""), "embedding": None})
        for i, s in enumerate(group):
            obs.append({"obs_id": f"{sid}_o{i}", "space_id": sid, "source": "dashcam", "camera_id": job["camera_id"],
                        "segment_source": s["segment_source"], "t_sec": float(s["t_global"]), "state": s["state"],
                        "confidence": None if s["confidence"] is None else float(s["confidence"])})
        steps = [
            ("gate", {"job_id": jid, "filename": job.get("filename"), "object_key": job.get("object_key"),
                      "keyframe": best["kf_index"], "segment_source": best["segment_source"], "t": best["t"],
                      "frame_index": best["frame_index"], "rule": best["gate"],
                      "evidence_start_s": best["t"], "evidence_end_s": min(5.0, best["t"] + 1.0)}),
            ("boxes", {"job_id": jid, "n_vehicle_boxes": best["n_vehicle_boxes"], "min_conf": MIN_CONF,
                       "classes": sorted(VEHICLES), "side_boxes": best["objs"], "frame_size": [1920, 1080]}),
            ("geometry", {"job_id": jid, "kind": best["kind"], "box": best["box"], "frame_size": [1920, 1080],
                          "ratio": best.get("ratio"), "gap_px": best.get("gap_px"), "neighbours": best.get("neighbours"),
                          "road": best["road"], "calibration": result["stats"]["calibration"],
                          "result": best["state"] if best["decided_by"] == "geometry" else "candidate"}),
            ("rule", {"job_id": jid, "hits": best["rule_hits"], "radii_m": RULE_RADII, "map": result["stats"]["map"],
                      "decided": best["decided_by"] == "rule", "state": best["state"]}),
        ]
        if best.get("cosmos") or best["decided_by"] == "cosmos":
            steps.append(("cosmos", {"job_id": jid, "sent": bool(best.get("cosmos")), **(best.get("cosmos") or {}),
                                     "min_p": COSMOS_MIN_P, "state": best["state"], "reason": best["reason"]}))
        steps.append(("position", {"job_id": jid, **best.get("pos_detail", {}), "lat": best["lat"], "lon": best["lon"],
                                   "status": best["position_status"], "sightings": len(group)}))
        for n, (step, det) in enumerate(steps, 1):
            trail.append({"trail_id": f"{sid}_{n}", "space_id": sid, "step_no": n, "step": step,
                          "detail_json": json.dumps(det), "at": at})
    return spaces, obs, trail
