"""Reject kerb slots that sit near mapped junctions, crossings, hydrants, or bus stops.

OpenStreetMap is queried only when data/pie_map.csv gives a position for the chunk.
One Overpass request per drive, cached under dashcam/out/osm/, at most one request
per second. With no position, the rule is recorded and nothing is rejected from a map.
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "data" / "pie_map.csv"
OSM_DIR = ROOT / "dashcam" / "out" / "osm"
NEAR_M = 15.0
OVERPASS = "https://overpass-api.de/api/interpreter"
LAT_KEYS = ("lat", "latitude")
LON_KEYS = ("lon", "lng", "longitude")


def _column(fields: list[str], options: tuple[str, ...]) -> str | None:
    for name in options:
        if name in fields:
            return name
    return None


def load_positions(path: Path = MAP_PATH) -> dict[tuple[str, int], tuple[float, float]] | None:
    if not path.is_file():
        return None
    import csv

    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        lat_key = _column(fields, LAT_KEYS)
        lon_key = _column(fields, LON_KEYS)
        if lat_key is None or lon_key is None or "set" not in fields or "chunk" not in fields:
            raise SystemExit(
                f"{path} has no set, chunk, and lat/lon columns (found {fields})"
            )
        positions: dict[tuple[str, int], tuple[float, float]] = {}
        for row in reader:
            positions[(row["set"], int(row["chunk"]))] = (float(row[lat_key]), float(row[lon_key]))
    return positions


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _throttle() -> None:
    OSM_DIR.mkdir(parents=True, exist_ok=True)
    stamp = OSM_DIR / ".last_request"
    if stamp.is_file():
        wait = 1.0 - (time.time() - stamp.stat().st_mtime)
        if wait > 0:
            time.sleep(wait)
    stamp.write_text("1")


def _points_of(element: dict) -> list[tuple[float, float]]:
    if "lat" in element and "lon" in element:
        return [(float(element["lat"]), float(element["lon"]))]
    geometry = element.get("geometry") or []
    return [(float(point["lat"]), float(point["lon"])) for point in geometry if "lat" in point]


def _kind(tags: dict) -> str | None:
    if tags.get("emergency") == "fire_hydrant":
        return "hydrant"
    if tags.get("highway") in {"bus_stop", "platform"} or tags.get("public_transport") == "platform":
        return "bus_stop"
    if tags.get("highway") == "crossing" or "crossing" in tags:
        return "crossing"
    if tags.get("highway") == "traffic_signals":
        return "junction"
    if tags.get("service") == "driveway" or (
        tags.get("highway") == "service" and tags.get("service") == "driveway"
    ):
        return "driveway"
    return None


def features_for_drive(set_id: str, positions: dict[tuple[str, int], tuple[float, float]]) -> list[dict]:
    points = [positions[key] for key in positions if key[0] == set_id]
    cache = OSM_DIR / f"{set_id}.json"
    if cache.is_file():
        return json.loads(cache.read_text())
    if not points:
        return []
    pad = 0.002
    south = min(lat for lat, _ in points) - pad
    north = max(lat for lat, _ in points) + pad
    west = min(lon for _, lon in points) - pad
    east = max(lon for _, lon in points) + pad
    bbox = f"{south},{west},{north},{east}"
    query = f"""
[out:json][timeout:60];
(
  node["highway"="bus_stop"]({bbox});
  node["public_transport"="platform"]({bbox});
  node["emergency"="fire_hydrant"]({bbox});
  node["highway"="crossing"]({bbox});
  node["crossing"]({bbox});
  node["highway"="traffic_signals"]({bbox});
  way["highway"="crossing"]({bbox});
  way["service"="driveway"]({bbox});
);
out geom;
"""
    _throttle()
    data = urllib.parse.urlencode({"data": query}).encode()
    request = urllib.request.Request(OVERPASS, data=data)
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = json.load(response)
    features = []
    for element in payload.get("elements") or []:
        kind = _kind(element.get("tags") or {})
        if kind is None:
            continue
        for lat, lon in _points_of(element):
            features.append({"kind": kind, "lat": lat, "lon": lon})
    cache.write_text(json.dumps(features))
    return features


def rejection(lat: float | None, lon: float | None, features: list[dict] | None) -> tuple[str, str]:
    """Return (reason, detail). reason is empty when the slot is not rejected."""
    if lat is None or lon is None or features is None:
        return "", "no position in data/pie_map.csv; OpenStreetMap not applied"
    nearest: dict[str, float] = {}
    for feature in features:
        distance = _haversine_m(lat, lon, feature["lat"], feature["lon"])
        kind = feature["kind"]
        if kind not in nearest or distance < nearest[kind]:
            nearest[kind] = distance
    for kind in ("junction", "crossing", "hydrant", "bus_stop", "driveway"):
        distance = nearest.get(kind)
        if distance is not None and distance <= NEAR_M:
            return (
                f"{kind}_within_{int(NEAR_M)}m",
                f"nearest {kind} is {distance:.1f} m; reject inside {int(NEAR_M)} m",
            )
    return "", "no junction, crossing, hydrant, bus stop, or driveway inside " + f"{int(NEAR_M)} m"
