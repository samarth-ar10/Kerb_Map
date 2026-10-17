"""Why "cannot tell"? Breakdown, before/after for the row rule, example frames, and label scoring.

  python dashcam/cannot_tell.py breakdown WORK_DIR                    # final reason + lane-unclear conditions
  python dashcam/cannot_tell.py compare BEFORE_WORK AFTER_WORK         # slot and receipt states, both versions
  python dashcam/cannot_tell.py examples OUT_DIR                      # 6 cannot_tell frames from the label batches
  python dashcam/cannot_tell.py score BEFORE_WORK AFTER_WORK          # needs answers_B*.csv in returned/

WORK_DIR = a receipts.py work folder (<set>_slots.json after the cosmos stage). Read-only on the lab SQLite.
Scoring: tune on set01-03, report on set04-06 (split by drive, never tune on the report half).
"""
import base64, collections, csv, glob, json, os, pathlib, re, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
SETS = ["set01", "set02", "set03", "set04", "set05", "set06"]
TUNE, REPORT = SETS[:3], SETS[3:]
LABELS = pathlib.Path(os.environ.get("DASHCAM_LABELS", HERE.parents[1] / "day-of" / "label-batches-dashcam"))


def load(work):
    return {s: json.loads((pathlib.Path(work) / f"{s}_slots.json").read_text()) for s in SETS}


def band(v, edges, labels):
    for e, l in zip(edges, labels):
        if v < e:
            return l
    return labels[-1]


def final_reason(x):
    r = x["reason"]
    if r.startswith("lane unclear: a car of unclear lane"):
        return "lane unclear (gap behind a car of unclear lane)"
    if r.startswith("lane unclear"):
        return "lane unclear (car)"
    for k, name in (("kerb hidden", "kerb hidden by traffic in our lane"), ("low confidence", "model low confidence"),
                    ("model unavailable", "model unavailable"), ("frame unavailable", "frame unavailable for model"),
                    ("not sent to model", "not sent to model (5% budget)"), ("OSM rules unavailable", "OSM unavailable"),
                    ("position uncertain", "position uncertain"), ("not one of the four", "model answer unparsed")):
        if k in r:
            return name
    return "other: " + r[:40]


def breakdown(work):
    segs = {r["segment_source"]: r for r in csv.DictReader(open(HERE.parent / "data" / "pie_segments.csv", encoding="utf-8"))}
    C = collections.defaultdict(collections.Counter)
    for s, d in load(work).items():
        for x in d["slots"]:
            C["state"][x["state"]] += 1
            if x["state"] != "cannot_tell":
                continue
            C["final_reason"][final_reason(x)] += 1
            C["final_reason_by_drive"][f"{s} {final_reason(x)}"] += 1
            if x["kind"] != "unclear_car":
                continue
            o = next(o for o in x["objs"] if o["bbox"] == x["box"])
            need = float(re.search(r"needs >= ([\d.]+)", x["reason"]).group(1))
            kerb_out = max(q["lat_out_m"] for q in x["objs"] if q["ego_overlap"] < 0.3)
            fi, fo = o["lat_in_m"] < need, o["lat_out_m"] < kerb_out - 2
            C["trigger"][" + ".join(t for t, f in (("inner edge closer than the lane margin", fi),
                                                   ("not in the outermost row", fo)) if f)] += 1
            C["lane_margin"][{5.0: "5.0 m: left side of a two-way road", 4.5: "4.5 m: multi-lane road",
                              1.8: "1.8 m: single lane our side"}[need]] += 1
            C["side"]["left" if x["side"] == "L" else "right"] += 1
            C["shortfall_vs_margin_m"][band(need - o["lat_in_m"], [0, 0.5, 1, 2, 3, 99],
                                            ["meets margin (row test failed)", "<0.5", "0.5-1", "1-2", "2-3", ">=3"])] += 1
            C["box_bottom_row_px"][band(x["box"][3], [760, 800, 850, 900, 1000],
                                        ["<760", "760-799", "800-849", "850-899", "900-999", ">=1000"])] += 1
            C["box_centre_offset_px"][band(abs((x["box"][0] + x["box"][2]) / 2 - 960), [200, 400, 600, 800],
                                           ["0-199", "200-399", "400-599", "600-799", ">=800"])] += 1
            z = x.get("pos_detail", {}).get("forward_m")
            C["depth_m"][band(z if z is not None else -1, [0, 10, 15, 20, 30, 40],
                              ["n/a", "<10", "10-15", "15-20", "20-30", "30-39.9", "40 (cap)"])] += 1
            try:
                kmh = float(segs[x["segment_source"]]["dist_m"]) / 5 * 3.6
            except (KeyError, TypeError, ValueError):
                kmh = -1
            C["speed_kmh"][band(kmh, [0, 5, 15, 30, 45], ["n/a", "0-4", "5-14", "15-29", "30-44", ">=45"])] += 1
            C["parked_row_same_side_same_frame"]["yes" if any(q.get("role") == "parked" for q in x["objs"]) else "no"] += 1
    out = {k: dict(v.most_common()) for k, v in C.items()}
    print(json.dumps(out, indent=1))
    return out


def key(s):
    return (s["segment_source"], round(float(s["t"]), 2), s["side"], tuple(int(round(float(v))) for v in s["box"]))


def receipts_of(d):
    import receipts
    spaces = receipts.dedupe([x for x in d["slots"] if x["state"] in ("free", "occupied", "not_space", "cannot_tell")])
    rec, slot2rec = [], {}
    for g in spaces:
        best = min(g, key=lambda s: s.get("pos_detail", {}).get("forward_m", 99))
        rec.append(best["state"])
        for s in g:
            slot2rec[key(s)] = best["state"]
    return rec, slot2rec


def compare(before, after):
    B, A = load(before), load(after)
    res = {}
    for name, D in (("before", B), ("after", A)):
        sl, rc, reasons, cosmos = collections.Counter(), collections.Counter(), collections.Counter(), 0
        for s, d in D.items():
            sl.update(x["state"] for x in d["slots"])
            reasons.update(final_reason(x) for x in d["slots"] if x["state"] == "cannot_tell")
            cosmos += d["stats"].get("cosmos_calls") or 0
            rc.update(receipts_of(d)[0])
        res[name] = {"slots": sum(sl.values()), "slots_by_state": dict(sl), "receipts": sum(rc.values()),
                     "receipts_by_state": dict(rc), "cannot_tell_reasons": dict(reasons), "cosmos_calls": cosmos}
    changed = collections.Counter()
    for s in SETS:
        bmap = {key(x): x for x in B[s]["slots"]}
        for x in A[s]["slots"]:
            b = bmap.get(key(x))
            if b is None or b["state"] != x["state"]:
                changed[f"{b['state'] if b else 'new slot'} -> {x['state']}"] += 1
    res["slot_changes"] = dict(changed)
    print(json.dumps(res, indent=1))
    return res


def manifest():
    m = {}
    for r in csv.DictReader(open(LABELS / "manifest.csv", encoding="utf-8")):
        m.setdefault(r["item_id"], r)
    return m


def examples(out_dir):
    """Six cannot_tell dc_ spots, one per trigger, pictures cut from the label batch pages (frame + box as sent)."""
    out = pathlib.Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    man = manifest()
    imgs = {}
    for f in sorted(LABELS.glob("label_batch_B*.html")):
        s = f.read_text(encoding="utf-8")
        m = re.search(r'<script[^>]*id="data"[^>]*>(.*?)</script>', s, re.S)
        for it in json.loads(m.group(1)):
            imgs.setdefault(it.get("item_id") or it.get("id"), it.get("img"))
    want = [("left of two-way road, inner edge short", lambda r: "two-way" in r),
            ("multi-lane road, inner edge short", lambda r: "multi-lane" in r),
            ("single lane, inner edge short", lambda r: "single lane" in r),
            ("gap behind a car of unclear lane", lambda r: "sits in front of this gap" in r),
            ("kerb hidden by traffic", lambda r: "kerb hidden" in r),
            ("left of two-way road, inner edge short (second)", lambda r: "two-way" in r)]
    used, rows = set(), []
    for label, test in want:
        for iid, r in man.items():
            if r["pipeline"] == "dc" and r["system_state"] == "cannot_tell" and iid not in used and imgs.get(iid) \
                    and test(r["system_reason"]):
                used.add(iid)
                fn = f"{len(rows) + 1}_{iid}.jpg"
                (out / fn).write_bytes(base64.b64decode(imgs[iid]))
                rows.append({"file": fn, "item_id": iid, "trigger": label, "drive": r["drive"], "side": r["side"],
                             "street": r["street"], "reason": r["system_reason"], "segment": r["segment_source"],
                             "t_sec": r["t_sec"], "box": [r["box_x1"], r["box_y1"], r["box_x2"], r["box_y2"]]})
                break
    (out / "examples.json").write_text(json.dumps(rows, indent=1))
    print(json.dumps(rows, indent=1))
    return rows


def answers():
    rows = []
    for f in sorted(glob.glob(str(LABELS / "returned" / "answers_B*.csv"))):
        for r in csv.DictReader(open(f, encoding="utf-8-sig")):
            if r.get("item_id", "").startswith("dc_") and r.get("human_label") in ("free", "occupied", "not_space"):
                rows.append(r)
    lab = collections.defaultdict(list)
    for r in rows:
        lab[r["item_id"]].append(r["human_label"])
    out = {}   # one label per spot: majority; ties (two people disagree on an overlap spot) are dropped
    for iid, ls in lab.items():
        c = collections.Counter(ls).most_common()
        if len(c) == 1 or c[0][1] > c[1][1]:
            out[iid] = c[0][0]
    return out


def prf(pairs, pos):
    """pairs: (system, human). pos: set of states counted as positive (real_space = {free, occupied})."""
    tp = sum(1 for s, h in pairs if s in pos and h in pos and (s == h or len(pos) > 1))
    sp = sum(1 for s, h in pairs if s in pos)
    hp = sum(1 for s, h in pairs if h in pos)
    return {"tp": tp, "system_said": sp, "human_said": hp,
            "precision": round(tp / sp, 3) if sp else None, "recall": round(tp / hp, 3) if hp else None}


def score(before, after):
    lab = answers()
    if not lab:
        msg = f"no dashcam answers yet in {LABELS / 'returned'} (answers_B*.csv); nothing scored"
        print(json.dumps({"status": "no labels", "detail": msg}))
        return
    man = manifest()
    res = {"status": "scored", "split": {"tune": TUNE, "report": REPORT}, "labelled_spots": len(lab)}
    for name, D in (("before", load(before)), ("after", load(after))):
        maps = {s: receipts_of(d)[1] for s, d in D.items()}
        for half, sets in (("tune", TUNE), ("report", REPORT)):
            pairs, unmatched = [], 0
            for iid, h in lab.items():
                r = man.get(iid)
                if not r or r["drive"] not in sets:
                    continue
                k = (r["segment_source"], round(float(r["t_sec"]), 2), r["side"],
                     tuple(int(round(float(r[c]))) for c in ("box_x1", "box_y1", "box_x2", "box_y2")))
                sysst = maps[r["drive"]].get(k)
                if sysst is None:   # box not matched: fall back to the shipped state, and count it
                    sysst, unmatched = r["system_state"], unmatched + 1
                pairs.append((sysst, h))
            res[f"{name}_{half}"] = {"n": len(pairs), "unmatched_boxes": unmatched,
                                     "free": prf(pairs, {"free"}), "occupied": prf(pairs, {"occupied"}),
                                     "real_space": prf(pairs, {"free", "occupied"}),
                                     "cannot_tell_answers": sum(1 for s, _ in pairs if s == "cannot_tell")}
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    {"breakdown": breakdown, "compare": compare, "examples": examples, "score": score}[cmd](*args)
