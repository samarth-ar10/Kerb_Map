"""Write query/samples_gps.json: GPS tracks of the prepared demo clips, shipped flat with the app.

    python query/make_samples_gps.py [path/to/samples.json]

Sources (both real PIE GPS):
  1. samples.json of the demo clips (default: $KERB_DEMO_SAMPLES, else ../day-of/demo-samples/samples.json next to
     the repo): entries with sample_id, filename and a GPS CSV (t_sec,lat,lon,heading_deg,speed_kmh, t from clip start).
  2. Whole 30 s PIE chunks from data/pie_segments.csv (start/end of every 5 s segment), for uploading an indexed chunk
     file itself (sample ids pie_<set>_<chunk>).
"""

import csv
import json
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(REPO, "query", "samples_gps.json")
PIE_CHUNKS = [("set01", 24), ("set04", 5), ("set04", 2)]
OSM_DIR = os.environ.get("DASHCAM_OSM", "C:/Users/samar/bystander-data/dashcam-osm")
OSM_MARGIN = 0.0008   # ~90 m around the clip's GPS track


def osm_for(set_name, gps):
    """OpenStreetMap elements around a track, cut from the cached Overpass file of that PIE drive (osm_fetch.py),
    so the kerb rules run on stage without a live Overpass call. None if no cache."""
    path = os.path.join(OSM_DIR, f"{set_name}.json")
    if not set_name or not gps or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        els = json.load(fh)["elements"]
    m = OSM_MARGIN
    b = (min(p[1] for p in gps) - m, min(p[2] for p in gps) - m, max(p[1] for p in gps) + m, max(p[2] for p in gps) + m)
    nodes = {e["id"]: e for e in els if e["type"] == "node"}

    def inside(n):
        return "lat" in n and b[0] <= n["lat"] <= b[2] and b[1] <= n["lon"] <= b[3]
    ways = [e for e in els if e["type"] == "way" and any(inside(nodes.get(i, {})) for i in e.get("nodes", []))]
    keep = {i for w in ways for i in w["nodes"]}
    out = [{"type": "way", "id": w["id"], "nodes": w["nodes"],
            "tags": {k: v for k, v in w.get("tags", {}).items() if k in ("highway", "name", "oneway", "lanes")}}
           for w in ways]
    for n in nodes.values():
        if n["id"] in keep or (inside(n) and n.get("tags")):
            tags = {k: v for k, v in (n.get("tags") or {}).items()
                    if k in ("highway", "crossing", "public_transport", "railway", "emergency")}
            out.append({"type": "node", "id": n["id"], "lat": n["lat"], "lon": n["lon"], **({"tags": tags} if tags else {})})
    return out


def fnum(v):
    try:
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


def pie_chunk(set_name, chunk):
    rows = [r for r in csv.DictReader(open(os.path.join(REPO, "data", "pie_segments.csv"), encoding="utf-8"))
            if r["set"] == set_name and int(r["chunk"]) == chunk]
    rows.sort(key=lambda r: int(r["segment"]))
    gps = []
    for r in rows:
        if (fnum(r["gps_valid_frac"]) or 0) < 0.5:
            continue
        t0 = (int(r["segment"]) - 1) * 5.0
        hd, sp = fnum(r["heading_mean_deg"]), fnum(r["speed_mean_kmh"])
        for t, la, lo in ((t0, r["lat_start"], r["lon_start"]), (t0 + 4.99, r["lat_end"], r["lon_end"])):
            if fnum(la) is not None and fnum(lo) is not None:
                gps.append([round(t, 2), fnum(la), fnum(lo), hd, sp])
    if not rows:
        return None
    return {"filename": rows[0]["filename"], "label": f"PIE {set_name} chunk {chunk:04d} ({rows[0]['street_main']})",
            "pie_video": rows[0]["pie_video"], "source": "data/pie_segments.csv (segment start/end)", "gps": gps}


def demo_samples(path):
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    items = data.get("samples", data) if isinstance(data, dict) else data
    out = {}
    for s in items:
        sid = None if s.get("sample_id") is None else str(s["sample_id"])
        gps_path = s.get("gps_csv") or s.get("gps") or s.get("gps_path")
        if not sid or not gps_path:
            continue
        if not os.path.isabs(gps_path):
            gps_path = os.path.join(os.path.dirname(path), gps_path)
        gps = []
        with open(gps_path, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                t, la, lo = fnum(r.get("t_sec")), fnum(r.get("lat")), fnum(r.get("lon"))
                if None not in (t, la, lo):
                    gps.append([t, la, lo, fnum(r.get("heading_deg")), fnum(r.get("speed_kmh"))])
        out[sid] = {"filename": s.get("filename"), "label": s.get("label") or s.get("street") or sid,
                    "pie_video": s.get("pie_video"), "start": s.get("start", s.get("start_s")),
                    "end": s.get("end", s.get("end_s")), "source": os.path.basename(gps_path), "gps": gps,
                    "osm": osm_for(s.get("pie_set"), gps)}
    return out


def main():
    default = os.environ.get("KERB_DEMO_SAMPLES") or os.path.join(REPO, "..", "..", "day-of", "demo-samples",
                                                                  "samples.json")
    path = sys.argv[1] if len(sys.argv) > 1 else default
    samples = demo_samples(path)
    for set_name, chunk in PIE_CHUNKS:
        s = pie_chunk(set_name, chunk)
        if s:
            samples.setdefault(f"pie_{set_name}_{chunk:04d}", s)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"note": "GPS of prepared demo clips (t_sec from clip start, lat, lon, heading_deg, speed_kmh)",
                   "samples": samples}, fh, separators=(",", ":"))
        fh.write("\n")
    print(f"{len(samples)} samples -> {os.path.relpath(OUT, REPO)} "
          f"({'with' if os.path.isfile(path) else 'without'} {path})")


if __name__ == "__main__":
    main()
