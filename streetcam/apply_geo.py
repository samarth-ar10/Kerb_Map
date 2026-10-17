"""Write homography results onto kerb_spaces and a position trail step.

A camera with no lat/lon in video_geo.csv is left without coordinates.
A fit whose held-out check is over 15 m does not replace coordinates.
position_status is ok only when that check is under 2 m.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import geo

ROOT = Path(__file__).resolve().parents[1]
# Overridden by argv.
STRETCH = ("right_curb", "left_curb", "near_between_driveways", "far_curb")


def stretch_of(space_id):
    for name in STRETCH:
        if f"_{name}_t" in space_id:
            return name
    return None


def main():
    spaces_path = Path(sys.argv[1])
    trail_path = Path(sys.argv[2])
    out_dir = Path(sys.argv[3])
    specs = [json.loads(Path(p).read_text()) for p in sys.argv[4:]]
    fits = {}
    for spec in specs:
        fits[spec["camera_id"]] = {"spec": spec, "fit": geo.fit_camera(spec)}
    (out_dir / "geo_fit.json").write_text(json.dumps(
        {cid: item["fit"] for cid, item in fits.items()}, indent=2
    ))

    # Cameras we were asked about that have no reference points.
    known = set(fits)
    spaces_out = []
    trail_add = []
    counts = {}
    for line in spaces_path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cam = row["camera_id"]
        name = stretch_of(row["space_id"])
        fit = fits.get(cam, {}).get("fit")
        counts.setdefault(cam, {"rows": 0, "moved": 0, "status": None})
        counts[cam]["rows"] += 1
        note = None
        if fit is None:
            note = "No map position: video_geo.csv has no lat/lon for this camera."
            row["position_status"] = "uncertain"
        else:
            check = fit["check"][0]["error_m"] if fit["check"] else None
            counts[cam]["status"] = fit["position_status"]
            counts[cam]["check_m"] = check
            stretch = (fit["stretches"] or {}).get(name) if name else None
            use = (
                stretch is not None
                and check is not None
                and check < 15
                and 1.0 <= stretch["length_m"] <= 40.0
            )
            if use:
                row["lat"] = stretch["lat"]
                row["lon"] = stretch["lon"]
                row["length_m"] = stretch["length_m"]
                row["street"] = stretch["street"]
                row["position_status"] = fit["position_status"]
                counts[cam]["moved"] += 1
                note = (
                    f"Map {stretch['lat']:.6f},{stretch['lon']:.6f} on {stretch['street']}. "
                    f"position_status {fit['position_status']}. "
                    f"Held-out check {check:.2f} m (ok only under 2 m)."
                )
            else:
                row["position_status"] = "uncertain"
                note = (
                    f"Homography check {check:.2f} m, over 2 m, so this row keeps its "
                    f"previous position and position_status uncertain."
                )
        if note and note not in (row.get("receipt_text") or ""):
            row["receipt_text"] = (row.get("receipt_text") or "").rstrip() + " " + note
        spaces_out.append(row)
        trail_add.append({
            "trail_id": row["space_id"] + "_4",
            "space_id": row["space_id"],
            "step_no": 4,
            "step": "position",
            "detail_json": json.dumps({"note": note, "position_status": row["position_status"],
                                       "lat": row.get("lat"), "lon": row.get("lon"),
                                       "street": row.get("street"), "length_m": row.get("length_m")}),
            "at": row.get("last_seen"),
        })

    spaces_path.write_text("".join(json.dumps(r) + "\n" for r in spaces_out))
    existing = trail_path.read_text()
    if not existing.endswith("\n"):
        existing += "\n"
    trail_path.write_text(existing + "".join(json.dumps(r) + "\n" for r in trail_add))
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
