"""Score rule variants against the human dashcam labels, split by drive.

  python dashcam/rules_from_labels.py score RUNS_DIR [variant ...]    # RUNS_DIR/<variant>/<set>_slots.json
  python dashcam/rules_from_labels.py export WORK_DIR OUT_DIR          # receipts JSONL per drive, no database writes

Each variant folder is a receipts.py work folder (slots + cosmos stages) run with one flag changed (see the flag block
at the top of receipts.py). Labels: day-of/label-batches-dashcam/returned/answers_B*.csv, dc_ spots only, human
FREE / OCCUPIED / NOT A SPACE (CAN'T TELL and skipped are left out; two people disagreeing on a spot drops it).
A labelled box that no longer exists in a variant (e.g. a gap between cars that became travel-lane cars) counts as
not_space, and is counted as "gone".
"""
import collections, csv, glob, json, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
import cannot_tell as CT   # noqa: E402

SETS = CT.SETS
HALVES = {"set01-03": SETS[:3], "set04-06": SETS[3:]}


def per_person():
    """Labels per (item, labeller) and pairwise agreement where two people answered the same spot."""
    by = collections.defaultdict(dict)
    for f in sorted(glob.glob(str(CT.LABELS / "returned" / "answers_B*.csv"))):
        for r in csv.DictReader(open(f, encoding="utf-8-sig")):
            if r.get("human_label") in ("free", "occupied", "not_space", "cant_tell"):
                by[r["item_id"]][r.get("labeller", "?")] = r["human_label"]
    pairs = collections.Counter()
    for iid, d in by.items():
        ppl = sorted(d)
        for i in range(len(ppl)):
            for j in range(i + 1, len(ppl)):
                pairs[(ppl[i], ppl[j], d[ppl[i]] == d[ppl[j]])] += 1
    people = sorted({p for d in by.values() for p in d})
    return {"labellers": people, "answers": sum(len(d) for d in by.values()),
            "overlap_pairs": {f"{a} vs {b}": {"agree": pairs[(a, b, True)], "n": pairs[(a, b, True)] + pairs[(a, b, False)]}
                              for a, b in {(a, b) for a, b, _ in pairs}}}


def score_dir(work, lab, man):
    D = CT.load(work)
    out = {"slots": collections.Counter(), "receipts": collections.Counter()}
    maps = {}
    for s, d in D.items():
        out["slots"].update(x["state"] for x in d["slots"])
        rec, m = CT.receipts_of(d)
        out["receipts"].update(rec)
        maps[s] = m
    for half, sets in HALVES.items():
        pairs, gone, rows = [], 0, []
        for iid, h in sorted(lab.items()):
            r = man.get(iid)
            if not r or r["drive"] not in sets:
                continue
            k = (r["segment_source"], round(float(r["t_sec"]), 2), r["side"],
                 tuple(int(round(float(r[c]))) for c in ("box_x1", "box_y1", "box_x2", "box_y2")))
            st = maps[r["drive"]].get(k)
            if st is None:
                st, gone = "not_space", gone + 1
            pairs.append((st, h))
            rows.append((iid, st, h))
        out[half] = {"n": len(pairs), "gone": gone, "free": CT.prf(pairs, {"free"}),
                     "occupied": CT.prf(pairs, {"occupied"}), "real_space": CT.prf(pairs, {"free", "occupied"}),
                     "exact": sum(1 for s_, h in pairs if s_ == h), "system_cannot_tell": sum(1 for s_, _ in pairs if s_ == "cannot_tell"),
                     "rows": rows}
    out["slots"], out["receipts"] = dict(out["slots"]), dict(out["receipts"])
    return out


def score(runs, *variants):
    runs = pathlib.Path(runs)
    variants = variants or sorted(p.name for p in runs.iterdir() if p.is_dir())
    lab, man = CT.answers(), CT.manifest()
    res = {"labelled_dc_spots": len(lab), "by_drive": dict(collections.Counter(man[i]["drive"] for i in lab if i in man)),
           "people": per_person(), "variants": {}}
    for v in variants:
        res["variants"][v] = score_dir(runs / v, lab, man)
    print(json.dumps(res, indent=1))
    return res


def export(work, out_dir):
    """Receipts (one per merged kerb stretch, closest view decides) as JSONL per drive, like write_drive, no DB."""
    import receipts as R
    out = pathlib.Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    tot = collections.Counter()
    for s, d in CT.load(work).items():
        groups = R.dedupe([x for x in d["slots"] if x["state"] in ("free", "occupied", "not_space", "cannot_tell")])
        with open(out / f"{s}_kerb_spaces.jsonl", "w", encoding="utf-8") as fh:
            for g in groups:
                b = min(g, key=lambda x: x.get("pos_detail", {}).get("forward_m", 99))
                tot[b["state"]] += 1
                fh.write(json.dumps({
                    "space_id": g[0]["slot_id"], "source": "dashcam", "camera_id": b.get("camera_id", "pie_cam-3"),
                    "segment_number": b["segment"], "t_sec": float(b["t"]), "side": b["side"], "state": b["state"],
                    "reason": b["reason"], "confidence": b["confidence"], "lat": b["lat"], "lon": b["lon"],
                    "position_status": b["position_status"], "street": b["street"] or None,
                    "length_m": round(b["ratio"] * R.CAR_LEN_M, 1) if b["kind"] == "gap" else None,
                    "evidence_uri": b["segment_source"], "box": b["box"], "sightings": len(g),
                    "receipt_text": R.receipt_text(b, len(g))}) + "\n")
    (out / "summary.json").write_text(json.dumps({"receipts_by_state": dict(tot), "receipts": sum(tot.values())}, indent=1))
    print(json.dumps(dict(tot)))


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    {"score": score, "export": export}[cmd](*args)
