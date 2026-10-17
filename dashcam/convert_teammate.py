"""Convert the pie-receipts pipeline output (branch origin/dashcam/pie-receipts) to the six-table contract.

Reads dashcam/out/kerb_spaces.jsonl and kerb_trail.jsonl from that branch with `git show` (nothing checked out),
maps every receipt to common/kerb_schema.py, adds lat/lon by interpolating data/pie_segments.csv at the receipt
time, and writes kerb_spaces / kerb_observations / kerb_trail through common/kerbdb.py (KERB_DB / KERB_SQLITE).

- space_id gets the prefix "pr_" so these rows stay separate from this pipeline's own "dc_" receipts.
- state "rejected" -> "not_space"; side left/right -> L/R; video -> evidence_uri / segment_source; time_sec -> t_sec.
- Position is the camera's GPS at the receipt time (no side offset: that pipeline stored no kerb geometry in metres);
  position_status "ok" when the segment's GPS is valid, else "uncertain".
- Every trail row's detail_json carries "pipeline": "pie-receipts" and the original detail text.
Re-runs replace only the "pr_" rows. Usage (repo root): python dashcam/convert_teammate.py
"""
import collections, csv, datetime, json, os, pathlib, subprocess, sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ.setdefault("KERB_DB", "local")
os.environ.setdefault("KERB_SQLITE", r"C:\Users\samar\bystander-data\local-lab\kerb.sqlite")
from common import kerbdb  # noqa: E402
from common.kerb_schema import SchemaError, validate_row, to_arrow  # noqa: E402

BRANCH = "origin/dashcam/pie-receipts"
PREFIX = "pr_"
PIPE = "pie-receipts"
OUT = REPO / "dashcam" / "out"


def git_lines(path):
    txt = subprocess.run(["git", "show", f"{BRANCH}:{path}"], cwd=REPO, capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    return [json.loads(l) for l in txt.splitlines() if l.strip()]


def fnum(v):
    try:
        return float(v) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None


def main():
    at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    segs = {r["segment_source"]: r for r in csv.DictReader(open(REPO / "data" / "pie_segments.csv", encoding="utf-8"))}
    geo = {r["segment_source"]: r for r in csv.DictReader(open(REPO / "data" / "video_geo.csv", encoding="utf-8"))}
    spaces_in = git_lines("dashcam/out/kerb_spaces.jsonl")
    trail_in = git_lines("dashcam/out/kerb_trail.jsonl")
    trails = collections.defaultdict(list)
    for t in trail_in:
        trails[t["space_id"]].append(t)

    sp_rows, obs_rows, tr_rows, dropped = [], [], [], collections.Counter()
    positioned = 0
    for s in spaces_in:
        src = (s.get("video") or s.get("evidence_uri") or "").split("#", 1)[0]
        seg = segs.get(src)
        if not seg:
            dropped["segment not in data/pie_segments.csv"] += 1
            continue
        t = fnum(s.get("time_sec")) or 0.0
        la0, lo0, la1, lo1 = (fnum(seg[k]) for k in ("lat_start", "lon_start", "lat_end", "lon_end"))
        lat = lon = None
        status = "uncertain"
        if la0 is not None and lo0 is not None:
            if la1 is None or lo1 is None:
                la1, lo1 = la0, lo0
            f = min(1.0, max(0.0, t / 5.0))
            lat, lon = round(la0 + (la1 - la0) * f, 7), round(lo0 + (lo1 - lo0) * f, 7)
            if (fnum(seg["gps_valid_frac"]) or 0) >= 0.5:
                status = "ok"
        positioned += status == "ok"
        state = {"rejected": "not_space"}.get(s.get("state"), s.get("state"))
        side = {"left": "L", "right": "R"}.get(s.get("side"), s.get("side"))
        sid = PREFIX + s["space_id"]
        pos_txt = (f"Position {lat:.6f}, {lon:.6f} (camera GPS at {t:.1f} s, not offset to the kerb)."
                   if status == "ok" else "Position uncertain.")
        text = (s.get("receipt_text") or "").replace("Position uncertain.", "").strip()
        row = {
            "space_id": sid, "source": "dashcam", "camera_id": (geo.get(src) or {}).get("camera_id") or "pie_cam-3",
            "original_video": (geo.get(src) or {}).get("original_video") or None,
            "segment_number": int(float(s.get("segment_number") or seg["segment"])), "t_sec": float(t),
            "side": side, "state": state, "reason": (s.get("reason") or None),
            "confidence": fnum(s.get("confidence")), "lat": lat, "lon": lon, "position_status": status,
            "street": seg.get("street_main") or None, "length_m": None, "last_seen": at, "evidence_uri": src,
            "receipt_text": f"{text} {pos_txt} [pipeline: {PIPE}]".strip(), "embedding": None,
        }
        try:
            validate_row("kerb_spaces", row)
        except SchemaError as exc:
            dropped[f"kerb_spaces contract: {exc}"[:80]] += 1
            continue
        sp_rows.append(row)
        obs_rows.append({"obs_id": sid + "_o0", "space_id": sid, "source": "dashcam", "camera_id": row["camera_id"],
                         "segment_source": src, "t_sec": float(t), "state": state, "confidence": row["confidence"]})
        for n, tr in enumerate(trails.get(s["space_id"], []), 1):
            det = {"pipeline": PIPE, "detail": tr.get("detail"), "reason": tr.get("reason"),
                   "state": {"rejected": "not_space"}.get(tr.get("state"), tr.get("state")),
                   "confidence": tr.get("confidence"), "original_trail_id": tr.get("trail_id")}
            if tr.get("step") == "position":
                det.update(converted_lat=lat, converted_lon=lon, converted_status=status,
                           note="lat/lon added at conversion: data/pie_segments.csv interpolated at the receipt time")
            if tr.get("step") == "gate":
                det["original_last_seen"] = s.get("last_seen")
            tr_rows.append({"trail_id": PREFIX + tr["trail_id"], "space_id": sid, "step_no": n, "step": tr.get("step"),
                            "detail_json": json.dumps(det), "at": at})

    # contract check on the whole batch before touching the database
    to_arrow("kerb_spaces", sp_rows); to_arrow("kerb_observations", obs_rows); to_arrow("kerb_trail", tr_rows)
    db = kerbdb.get_db()
    db.ensure_tables()
    if getattr(db, "backend", "") == "local":
        with db._lock, db._conn:
            for tbl, k in (("kerb_spaces", "space_id"), ("kerb_observations", "obs_id"), ("kerb_trail", "trail_id")):
                db._conn.execute(f"DELETE FROM {tbl} WHERE {k} LIKE ?", (PREFIX + "%",))
    db.insert("kerb_spaces", sp_rows)
    db.insert("kerb_observations", obs_rows)
    db.insert("kerb_trail", tr_rows)
    for name, rows in (("spaces", sp_rows), ("observations", obs_rows), ("trail", tr_rows)):
        with open(OUT / f"pie_receipts_converted_kerb_{name}.jsonl", "w", encoding="utf-8", newline="\n") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    summary = {"read_spaces": len(spaces_in), "read_trail": len(trail_in), "converted_spaces": len(sp_rows),
               "observations": len(obs_rows), "trail_rows": len(tr_rows), "position_ok": positioned,
               "position_uncertain": len(sp_rows) - positioned, "dropped": dict(dropped),
               "states": dict(collections.Counter(r["state"] for r in sp_rows)),
               "sides": dict(collections.Counter(r["side"] for r in sp_rows)), "backend": db.backend}
    (OUT / "pie_receipts_converted_summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
