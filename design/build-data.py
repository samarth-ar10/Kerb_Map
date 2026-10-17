"""Build compact kerb sightings from the PIE GPS map. Run once."""
import json, math, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
html = (ROOT / "toronto-drives-map.html").read_text(encoding="utf-8")
m = re.search(r"const D = (\[.*?\]);\s*const COL", html, re.S)
D = json.loads(m.group(1))
R = 6371000.0


def dist_m(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dphi = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1, math.sqrt(h)))


def bearing(a, b):
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return round((math.degrees(math.atan2(y, x)) + 360) % 360, 1)


sightings = []
for s in D:
    pts = s["p"]
    if len(pts) < 2:
        continue
    a, b = pts[0], pts[-1]
    length = 0.0
    for p, q in zip(pts, pts[1:]):
        length += dist_m(p, q)
    length = max(length, max(0.5, (s["sp"] or 0) * (s["b"] - s["a"]) / 3.6))
    mid = pts[len(pts) // 2]
    sightings.append(
        [
            s["s"],
            s["c"],
            s["g"],
            s["v"],
            round(s["a"], 1),
            round(s["b"], 1),
            s.get("st") or "Unnamed road",
            s["cf"][0],  # h/m/l
            s["sp"],
            round(length, 1),
            round((6 - s["s"]) * 18 + (600 - s["b"]) / 60.0, 1),
            round(a[0], 5),
            round(a[1], 5),
            round(b[0], 5),
            round(b[1], 5),
            round(mid[0], 5),
            round(mid[1], 5),
            bearing(a, b),
        ]
    )

drives = []
for sid in sorted({x[0] for x in sightings}):
    xs = [x for x in sightings if x[0] == sid]
    streets = []
    for x in xs:
        if x[6] not in streets:
            streets.append(x[6])
    drives.append({"id": sid, "name": f"Drive set0{sid}", "n": len(xs), "streets": streets[:4]})

cameras = [
    {
        "id": "sf",
        "name": "San Francisco · Broadway & Columbus",
        "lat": 37.79665,
        "lng": -122.40583,
        "note": "Position set from landmarks in view — optional street-camera layer.",
        "stills": [
            "footage-samples/sf_streets_cam-5_0.jpg",
            "footage-samples/sf_streets_cam-5_1.jpg",
        ],
    },
    {
        "id": "nyc",
        "name": "New York · Broadway & Walker St",
        "lat": 40.71955,
        "lng": -74.00255,
        "note": "Position set from landmarks in view — optional street-camera layer.",
        "stills": [
            "footage-samples/nyc_streets_cam-1_0.jpg",
            "footage-samples/nyc_streets_cam-1_1.jpg",
        ],
    },
    {
        "id": "nbhd",
        "name": "Residential street camera (two days)",
        "lat": None,
        "lng": None,
        "note": "Keep off the Toronto map until landmarks are confirmed. Optional — removable.",
        "stills": [
            "footage-samples/neighborhood_cam-1_0_2.jpg",
            "footage-samples/neighborhood_cam-1_1_2.jpg",
            "footage-samples/neighborhood_cam-1_2_2.jpg",
            "footage-samples/neighborhood_cam-1_3.jpg",
        ],
    },
]

out = {
    "cols": "drive,chunk,seg,video,t0,t1,street,align,speed,len,ago,aLat,aLng,bLat,bLng,lat,lng,brg",
    "source": "PIE Toronto dashcam GPS (MIT licence). States unlabeled until the team marks them.",
    "drives": drives,
    "rows": sightings,
    "cameras": cameras,
    "stats": {
        "sightings": len(sightings),
        "drives": len(drives),
        "streets": len({x[6] for x in sightings}),
    },
}
js = "window.KERB_DATA=" + json.dumps(out, separators=(",", ":")) + ";\n"
(ROOT / "data.js").write_text(js, encoding="utf-8")
print("wrote", len(js), "bytes", "rows", len(sightings))
