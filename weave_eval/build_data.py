"""Regenerate weave_eval/data/*.json from the label CSVs and the stored per-variant scoring files.

No pipeline re-run: every variant's state per labelled spot was already computed by
dashcam/rules_from_labels.py (variant_scores.json, rows = [item_id, system_state, human_label]).

  python weave_eval/build_data.py --labels DIR/returned --scores-old OLD.json --scores-new NEW.json

--scores-old  variant_scores.json as committed on dashcam/cannot-tell (first labeller only, 28 spots)
--scores-new  variant_scores.json re-scored with both labellers (53 spots, ties dropped)
Spots both files cover must agree on every variant; the union gives a state for every labelled spot,
including the two spots the labellers disagree on.
"""
import argparse, collections, csv, glob, json, pathlib

HERE = pathlib.Path(__file__).resolve().parent
# our variant name -> key in variant_scores.json
VARIANTS = {"shipped": "shipped", "gap 0.85": "A_0.85", "travel-lane row": "C_row",
            "OSM no-parking": "C_nopark", "all three": "FINAL_tune0103"}
TUNING, HELDOUT = ("set01", "set02", "set03"), ("set04", "set05", "set06")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True)
    ap.add_argument("--scores-old", required=True)
    ap.add_argument("--scores-new", required=True)
    a = ap.parse_args()

    ans = collections.defaultdict(dict)   # item -> labeller -> label (decided dc_ answers only)
    meta = {}
    for f in sorted(glob.glob(str(pathlib.Path(a.labels) / "answers_B*.csv"))):
        for r in csv.DictReader(open(f, encoding="utf-8-sig")):
            if r["item_id"].startswith("dc_") and r["human_label"] in ("free", "occupied", "not_space"):
                ans[r["item_id"]][r["labeller"]] = r["human_label"]
                meta[r["item_id"]] = {"drive": r["item_id"].split("_")[1], "side": r["side"]}

    states = {k: {} for k in VARIANTS}
    for path, halves in ((a.scores_old, ("set01-03", "set04-06")), (a.scores_new, ("all",))):
        v = json.load(open(path))["variants"]
        for name, key in VARIANTS.items():
            for h in halves:
                for iid, st, _human in v[key][h]["rows"]:
                    if states[name].setdefault(iid, st) != st:
                        raise SystemExit(f"conflicting state for {iid} in {name}")
    missing = [i for i in ans if any(i not in states[n] for n in VARIANTS)]
    if missing:
        raise SystemExit(f"no stored state for {missing}")

    rows = []
    for iid in sorted(ans):
        labels = ans[iid]
        agreed = len(set(labels.values())) == 1
        for k, (who, lab) in enumerate(sorted(labels.items())):
            rows.append({"id": iid, "drive": meta[iid]["drive"], "side": meta[iid]["side"],
                         "split": "tuning" if meta[iid]["drive"] in TUNING else "held-out",
                         "labeller": who, "human_label": lab,
                         "combined": agreed and k == 0})   # in the both-labellers table once; disputed spots out
    (HERE / "data" / "labels.json").write_text(json.dumps(rows, indent=1))
    (HERE / "data" / "variant_states.json").write_text(json.dumps(
        {"variants": {n: dict(sorted(s.items())) for n, s in states.items()},
         "source": "dashcam/out/rules-from-labels/variant_scores.json (dashcam/cannot-tell), per labelled spot"}, indent=1))
    print(len(rows), "answers;", sum(r["combined"] for r in rows), "combined spots;",
          len(ans), "unique spots")


if __name__ == "__main__":
    main()
