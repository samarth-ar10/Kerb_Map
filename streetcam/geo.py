"""Ground-plane homography from image pixels to map coordinates.

Image points are pixels on the road. Map points are OpenStreetMap nodes, stored
as latitude and longitude and converted to local east/north metres for the fit.
position_status is "ok" only when every leave-one-out error and the held-out
check point are under MAX_ERROR_M. Otherwise it is "uncertain".
"""

import json
import math

MAX_ERROR_M = 2.0


def latlon_to_enu(lat, lon, lat0, lon0):
    east = math.radians(lon - lon0) * 6378137.0 * math.cos(math.radians(lat0))
    north = math.radians(lat - lat0) * 6378137.0
    return east, north


def enu_to_latlon(east, north, lat0, lon0):
    lat = lat0 + math.degrees(north / 6378137.0)
    lon = lon0 + math.degrees(east / (6378137.0 * math.cos(math.radians(lat0))))
    return lat, lon


def _solve(a_rows, b):
    """Solve a square linear system. a_rows is a list of lists."""
    n = len(b)
    m = [list(row) + [b[i]] for i, row in enumerate(a_rows)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise ValueError("singular homography")
        m[col], m[pivot] = m[pivot], m[col]
        scale = m[col][col]
        for j in range(col, n + 1):
            m[col][j] /= scale
        for r in range(n):
            if r == col:
                continue
            factor = m[r][col]
            for j in range(col, n + 1):
                m[r][j] -= factor * m[col][j]
    return [m[i][n] for i in range(n)]


def fit_homography(pixels, enu):
    """Least-squares homography, image (u, v) to local metres (east, north).

    Returns 8 coefficients with the last element of H fixed at 1.
    """
    if len(pixels) < 4:
        raise ValueError("need at least 4 reference points")
    rows = []
    b = []
    for (u, v), (x, y) in zip(pixels, enu):
        rows.append([u, v, 1, 0, 0, 0, -x * u, -x * v])
        b.append(x)
        rows.append([0, 0, 0, u, v, 1, -y * u, -y * v])
        b.append(y)
    # Normal equations.
    ata = [[0.0] * 8 for _ in range(8)]
    atb = [0.0] * 8
    for row, bi in zip(rows, b):
        for i in range(8):
            atb[i] += row[i] * bi
            for j in range(8):
                ata[i][j] += row[i] * row[j]
    return _solve(ata, atb)


def apply_h(h, u, v):
    x = h[0] * u + h[1] * v + h[2]
    y = h[3] * u + h[4] * v + h[5]
    w = h[6] * u + h[7] * v + 1.0
    if abs(w) < 1e-12:
        raise ValueError("homography hit the horizon")
    return x / w, y / w


def pixel_to_latlon(h, u, v, lat0, lon0):
    east, north = apply_h(h, u, v)
    return enu_to_latlon(east, north, lat0, lon0)


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def leave_one_out(pixels, enu):
    errors = []
    for i in range(len(pixels)):
        try:
            h = fit_homography(pixels[:i] + pixels[i + 1 :], enu[:i] + enu[i + 1 :])
            pred = apply_h(h, pixels[i][0], pixels[i][1])
            errors.append(_dist(pred, enu[i]))
        except ValueError:
            errors.append(None)
    return errors


def project_polyline(h, pixels, lat0, lon0):
    return [pixel_to_latlon(h, u, v, lat0, lon0) for u, v in pixels]


def polyline_length_m(latlons):
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(latlons, latlons[1:]):
        e1, n1 = latlon_to_enu(lat1, lon1, latlons[0][0], latlons[0][1])
        e2, n2 = latlon_to_enu(lat2, lon2, latlons[0][0], latlons[0][1])
        total += math.hypot(e2 - e1, n2 - n1)
    return total


def nearest_street(lat, lon, streets):
    """streets: list of {name, points: [[lat, lon], ...]}."""
    best_name = None
    best = None
    for street in streets:
        pts = street["points"]
        for (lat1, lon1), (lat2, lon2) in zip(pts, pts[1:]):
            # Distance from the point to this segment, in local metres.
            e, n = latlon_to_enu(lat, lon, lat1, lon1)
            e2, n2 = latlon_to_enu(lat2, lon2, lat1, lon1)
            seg = e2 * e2 + n2 * n2
            t = 0.0 if seg == 0 else max(0.0, min(1.0, (e * e2 + n * n2) / seg))
            d = math.hypot(e - t * e2, n - t * n2)
            if best is None or d < best:
                best = d
                best_name = street["name"]
    return best_name, best


def fit_camera(spec):
    """Fit one camera. spec has origin, fit[], check[], stretches{}, streets[]."""
    lat0 = spec["origin"]["lat"]
    lon0 = spec["origin"]["lon"]
    fit = spec["fit"]
    pixels = [(p["u"], p["v"]) for p in fit]
    enu = [latlon_to_enu(p["lat"], p["lon"], lat0, lon0) for p in fit]
    h = fit_homography(pixels, enu)
    loo = leave_one_out(pixels, enu)
    checks = []
    for p in spec.get("check") or []:
        pred = apply_h(h, p["u"], p["v"])
        true = latlon_to_enu(p["lat"], p["lon"], lat0, lon0)
        checks.append({"id": p["id"], "error_m": _dist(pred, true)})
    # A 4-point fit has no residual. The held-out check point is the error that
    # decides ok versus uncertain. Leave-one-out is reported when a subset still
    # has four points.
    check_errors = [c["error_m"] for c in checks]
    loo_errors = [e for e in loo if e is not None]
    if not check_errors:
        worst = None
        status = "uncertain"
    else:
        worst = max(check_errors + loo_errors)
        status = "ok" if worst < MAX_ERROR_M else "uncertain"
    stretches = {}
    for name, centerline in spec["stretches"].items():
        latlons = project_polyline(h, centerline, lat0, lon0)
        mid = latlons[len(latlons) // 2]
        street, street_m = nearest_street(mid[0], mid[1], spec.get("streets") or [])
        stretches[name] = {
            "lat": mid[0],
            "lon": mid[1],
            "length_m": polyline_length_m(latlons),
            "street": street,
            "street_distance_m": street_m,
            "vertices": latlons,
        }
    return {
        "position_status": status,
        "max_error_m": worst,
        "leave_one_out_m": loo,
        "check": checks,
        "stretches": stretches,
        "max_error_limit_m": MAX_ERROR_M,
    }


def self_test():
    # Known perspective. Six ground points, fit on all of them, residual must be ~0.
    h_true = [0.02, 0.001, -5.0, -0.001, 0.018, 8.0, 0.0002, -0.0001]
    pixels = [(120, 220), (480, 200), (520, 560), (90, 540), (280, 360), (200, 450)]
    enu = [apply_h(h_true, u, v) for u, v in pixels]
    h = fit_homography(pixels, enu)
    for (u, v), true in zip(pixels, enu):
        err = _dist(apply_h(h, u, v), true)
        if err > 1e-4:
            raise SystemExit(f"self-test residual {err}")
    loo = leave_one_out(pixels, enu)
    if any(e is None or e > 0.05 for e in loo):
        raise SystemExit(f"self-test leave-one-out {loo}")
    print("self-test ok", [round(v, 6) for v in loo])


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        self_test()
    elif len(sys.argv) == 2:
        spec = json.loads(open(sys.argv[1]).read())
        print(json.dumps(fit_camera(spec), indent=2))
    else:
        self_test()
