r"""Fetch OpenStreetMap features once per drive area (Overpass), cached as JSON.

One request per drive (bbox of its GPS + 60 m margin), at most 1 request/s, with a User-Agent.
Cache: <bystander-data>/dashcam-osm/<set>.json (raw Overpass output). Skips cached drives.
"""
import csv, json, os, pathlib, sys, time, collections
import requests

REPO = pathlib.Path(__file__).resolve().parents[1]
CACHE = pathlib.Path(os.environ.get("DASHCAM_OSM", r"C:\Users\samar\bystander-data\dashcam-osm"))
URLS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
UA = "KerbMap-hackathon/0.1 (team 5 dashcam receipts; contact via team repo)"

QUERY = """[out:json][timeout:90];
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


def bboxes():
    by = collections.defaultdict(list)
    for r in csv.DictReader(open(REPO / "data" / "pie_segments.csv")):
        for a, o in (("lat_start", "lon_start"), ("lat_end", "lon_end")):
            if r[a] and r[o]:
                by[r["set"]].append((float(r[a]), float(r[o])))
    m = 0.0006  # ~60 m
    return {s: (min(p[0] for p in v) - m, min(p[1] for p in v) - m, max(p[0] for p in v) + m, max(p[1] for p in v) + m)
            for s, v in sorted(by.items())}


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    want = sys.argv[1:] or None
    for s, b in bboxes().items():
        if want and s not in want:
            continue
        out = CACHE / f"{s}.json"
        if out.exists():
            print(s, "cached"); continue
        q = QUERY.format(b=",".join(f"{x:.6f}" for x in b))
        for attempt in range(6):
            r = requests.post(URLS[attempt % 2], data={"data": q}, headers={"User-Agent": UA}, timeout=120)
            if r.status_code == 200:
                out.write_text(r.text, encoding="utf-8")
                print(s, "ok", len(r.json().get("elements", [])), "elements")
                break
            print(s, "http", r.status_code, "retry"); time.sleep(5 * (attempt + 1))
        time.sleep(1.1)


if __name__ == "__main__":
    main()
