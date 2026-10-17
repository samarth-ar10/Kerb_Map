"""Pick about 120 of this pipeline's receipts for the team's human label pool -> dashcam/out/label_candidates.csv.

Priority per drive: every free, every rule-rejected (not_space by an OSM rule), every model-checked slot, then
cannot_tell (lane unclear / hidden / budget) and a few occupied, taken evenly across the drive (deterministic).
Columns: spot_id, set, segment_source, t_sec (in the 5 s segment), frame_index, box x1/y1/x2/y2 and point x/y
(bottom centre) in original 1920x1080 pixels, system_state, decided_by, reason.
"""
import csv, json, pathlib, collections

OUT = pathlib.Path(__file__).resolve().parent / "out"
SETS = ["set01", "set02", "set03", "set04", "set05", "set06"]
PER_SET = 20


def even(items, k):
    if k <= 0 or not items:
        return []
    if len(items) <= k:
        return items
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


def main():
    rows = []
    for s in SETS:
        sp = [json.loads(l) for l in open(OUT / f"{s}_kerb_spaces.jsonl", encoding="utf-8")]
        tr = collections.defaultdict(dict)
        for l in open(OUT / f"{s}_kerb_trail.jsonl", encoding="utf-8"):
            t = json.loads(l)
            tr[t["space_id"]][t["step"]] = json.loads(t["detail_json"])
        def by(fn):
            return sorted([x for x in sp if fn(x)], key=lambda x: x["space_id"])
        free = by(lambda x: x["state"] == "free")
        rule = by(lambda x: x["state"] == "not_space" and tr[x["space_id"]].get("rule", {}).get("decided"))
        model = by(lambda x: "cosmos" in tr[x["space_id"]] and x["state"] != "free")
        picked = {x["space_id"] for x in free + rule + model}
        ct_gap = by(lambda x: x["state"] == "cannot_tell" and tr[x["space_id"]]["geometry"]["kind"] == "gap"
                    and x["space_id"] not in picked)
        ct_car = by(lambda x: x["state"] == "cannot_tell" and tr[x["space_id"]]["geometry"]["kind"] != "gap")
        short = by(lambda x: x["state"] == "not_space" and x["space_id"] not in picked)
        occ = by(lambda x: x["state"] == "occupied")
        chosen = free + rule + model + ct_gap
        left = PER_SET - len(chosen)
        chosen += even(ct_car, max(0, left // 2)) + even(short, min(len(short), max(0, left // 4)))
        chosen += even(occ, max(0, PER_SET - len(chosen)))
        for x in chosen:
            g = tr[x["space_id"]]
            x1, y1, x2, y2 = g["geometry"]["box"]
            rows.append({"spot_id": x["space_id"], "set": s, "segment_source": x["evidence_uri"],
                         "t_sec": x["t_sec"], "frame_index": g["gate"]["frame_index"],
                         "box_x1": x1, "box_y1": y1, "box_x2": x2, "box_y2": y2,
                         "point_x": round((x1 + x2) / 2), "point_y": y2, "system_state": x["state"],
                         "decided_by": ("cosmos" if "cosmos" in g else ("rule" if g["rule"].get("decided") else "geometry")),
                         "reason": x["reason"]})
    with open(OUT / "label_candidates.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print(len(rows), collections.Counter(r["system_state"] for r in rows), collections.Counter(r["decided_by"] for r in rows))


if __name__ == "__main__":
    main()
