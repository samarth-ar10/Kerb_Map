"""Weave evaluation of the dashcam kerb rule variants, scored against human labels.

One variable changes at a time versus the shipped rules. Each variant is a weave.Model that returns the
state that variant stored for a labelled spot (precomputed by dashcam/rules_from_labels.py; no pipeline re-run).

  python weave_eval/kerb_rules_eval.py              # log all variants to W&B Weave (needs WANDB_* env, see below)
  python weave_eval/kerb_rules_eval.py --dry-run    # compute everything locally, never contact W&B
                                                    # (also writes comparison.md / comparison.csv)

Env for the real run: WANDB_API_KEY, WANDB_TEAM (entity), WANDB_PROJECT. Nothing is hard-coded.
comparison.md / .csv are always computed offline from the same numbers, before any W&B call.

Metrics (per variant, per group): precision = right / system said, recall = right / human said.
  free_pr, occupied_precision (+recall), real_space_precision (+recall; real space = free or occupied),
  agreement (system state == human label; cannot_tell counts as wrong).
Groups: tuning (set01-03) and held-out (set04-06) both-labellers; all; and each labeller's own answers.
A spot both labellers answered the same way counts once in the both-labellers groups; a spot they disagree
on is left out of them (it stays in each person's own group).
"""
import argparse, csv, json, os, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "data"

# (display name, key in variant_states.json). Order = table order.
VARIANTS = [("rules: shipped", "shipped"),
            ("rules: +gap 0.85", "gap 0.85"),
            ("rules: +travel-lane row", "travel-lane row"),
            ("rules: +OSM no-parking travel lane", "OSM no-parking"),
            ("rules: all three (chosen)", "all three")]
GROUPS = ["all", "tuning (set01-03)", "held-out (set04-06)", "labeller: Atharva", "labeller: Builder C"]
REAL = {"free", "occupied"}
METRICS = [("free", "free P", "free R"), ("occupied", "occupied P", "occupied R"),
           ("real_space", "real-space P", "real-space R")]


# ---- pure scoring (no weave): the single source of truth for both the Weave scorers and the local tables ----
def in_group(row, group):
    if group.startswith("labeller: "):
        return row["labeller"] == group[len("labeller: "):]
    if not row["combined"]:
        return False
    return group == "all" or row["split"] == group.split(" ")[0]


def hit(kind, state, human):
    """-> (tp, system_said, human_said) for one row."""
    pos = REAL if kind == "real_space" else {kind}
    s, h = state in pos, human in pos
    return int(s and h), int(s), int(h)


def tally(rows, states, kind):
    """rows: dataset rows; states: {row_id: state}. -> {group: {tp, said, human, exact, n}}"""
    out = {}
    for g in GROUPS:
        t = {"n": 0, "tp": 0, "system_said": 0, "human_said": 0, "exact": 0}
        for r in rows:
            if in_group(r, g):
                tp, s, h = hit(kind, states[r["id"]], r["human_label"])
                t["n"] += 1; t["tp"] += tp; t["system_said"] += s; t["human_said"] += h
                t["exact"] += int(states[r["id"]] == r["human_label"])
        out[g] = t
    return out


def ratio(a, b):
    return round(a / b, 3) if b else None


def frac(a, b):
    return f"{a}/{b}"


def load():
    rows = json.load(open(DATA / "labels.json"))
    st = json.load(open(DATA / "variant_states.json"))["variants"]
    return rows, st


def offline_table():
    """{variant: {group: {n, exact, free:{tp,said,human}, ...}}} computed with no W&B / weave."""
    rows, st = load()
    res = {}
    for disp, key in VARIANTS:
        res[disp] = {}
        for kind, _, _ in METRICS:
            for g, t in tally(rows, st[key], kind).items():
                d = res[disp].setdefault(g, {"n": t["n"], "exact": t["exact"]})
                d[kind] = {"tp": t["tp"], "system_said": t["system_said"], "human_said": t["human_said"]}
    return res


STREETCAM = [("dev (A3, A6)", "eval_labels.json"), ("held-out (A1, A2, A4)", "eval_heldout.json")]


def streetcam_rows():
    """Old track rule vs position-based rule, from streetcam/out/eval_*.json (committed on origin/main)."""
    out = []
    for lab, fn in STREETCAM:
        for base in (HERE.parent / "streetcam" / "out", HERE / "data"):
            p = base / fn
            if p.exists():
                d = json.load(open(p))
                out.append({"split": lab, "n": d["n_items_scored"], "agree_before": d["agree_before"],
                            "agree_after": d["agree_after"], "fixed": d["fixed_by_change"],
                            "broken": d["broken_by_change"]})
                break
    return out


def write_local(res):
    cols = ["variant", "group", "n", "exact"] + [f"{k}_{x}" for k, _, _ in METRICS for x in ("tp", "system_said", "human_said")]
    with open(HERE / "comparison.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols + [f"{k}_{x}" for k, _, _ in METRICS for x in ("precision", "recall")])
        for v, gs in res.items():
            for g, d in gs.items():
                vals = [v, g, d["n"], d["exact"]]
                pr = []
                for k, _, _ in METRICS:
                    m = d[k]
                    vals += [m["tp"], m["system_said"], m["human_said"]]
                    pr += [ratio(m["tp"], m["system_said"]), ratio(m["tp"], m["human_said"])]
                w.writerow(vals + pr)
    L = ["# Kerb rules vs human labels", "",
         "One change per variant versus shipped; each cell is right / system said (precision) or right / human said (recall). "
         "`exact` = system state equals the human label (cannot_tell counts as wrong). Computed offline from the stored per-spot states; "
         "no pipeline re-run, no W&B. Tuning drives = set01-03, held-out drives = set04-06. Both labellers answered some set01-03 spots, so the tuning half is no longer purely the set the rules were tuned on. Two labellers, ~55 spots: one spot moves a cell by 8-30 points.", ""]
    for g in GROUPS:
        n = next(iter(res.values()))[g]["n"]
        L += [f"## {g} (n={n})", "",
              "| variant | " + " | ".join(m for _, a, b in METRICS for m in (a, b)) + " | exact |",
              "|---|" + "---|" * 7]
        for v, gs in res.items():
            d = gs[g]
            cells = []
            for k, _, _ in METRICS:
                m = d[k]
                cells += [frac(m["tp"], m["system_said"]), frac(m["tp"], m["human_said"])]
            L.append(f"| {v.replace('rules: ', '')} | " + " | ".join(cells) + f" | {d['exact']}/{d['n']} |")
        L.append("")
    sc = streetcam_rows()
    if sc:
        L += ["## Street camera: old track rule vs position-based (agreement with human labels)", "",
              "| split | n | old rule | position-based | fixed | broken |", "|---|---|---|---|---|---|"]
        for r in sc:
            L.append(f"| {r['split']} | {r['n']} | {r['agree_before']}/{r['n']} | {r['agree_after']}/{r['n']} | {r['fixed']} | {r['broken']} |")
        L += ["", "Not logged to Weave: the eval files hold confusion matrices, not per-item before/after states."]
    (HERE / "comparison.md").write_text("\n".join(L) + "\n", encoding="utf-8")


# ---- Weave objects ----
def build_weave():
    import weave

    class KerbRulesModel(weave.Model):
        """Returns the stored state of one rule variant for a labelled spot."""
        variant: str
        states: dict

        @weave.op
        def predict(self, id: str) -> dict:
            return {"state": self.states[id], "variant": self.variant}

    class Group(weave.Scorer):
        kind: str

        @weave.op
        def score(self, output: dict, id: str, human_label: str, labeller: str, split: str, combined: bool) -> dict:
            tp, s, h = hit(self.kind, output["state"], human_label)
            return {"tp": tp, "system_said": s, "human_said": h, "exact": int(output["state"] == human_label),
                    "labeller": labeller, "split": split, "combined": combined}

        @weave.op
        def summarize(self, score_rows: list) -> dict:
            out = {}
            for g in GROUPS:
                rs = [r for r in score_rows if in_group(r, g)]
                tp, s, h = (sum(r[k] for r in rs) for k in ("tp", "system_said", "human_said"))
                out[g] = {"precision": ratio(tp, s), "recall": ratio(tp, h), "tp": tp, "system_said": s,
                          "human_said": h, "n": len(rs)}
            return out

    class FreePR(Group):
        kind: str = "free"

    class OccupiedPrecision(Group):
        kind: str = "occupied"

    class RealSpacePrecision(Group):
        kind: str = "real_space"

    class Agreement(Group):
        kind: str = "real_space"

        @weave.op
        def summarize(self, score_rows: list) -> dict:
            return {g: {"agreement": ratio(sum(r["exact"] for r in rs), len(rs)),
                        "exact": sum(r["exact"] for r in rs), "n": len(rs)}
                    for g in GROUPS for rs in [[r for r in score_rows if in_group(r, g)]]}

    return weave, KerbRulesModel, [FreePR(), OccupiedPrecision(), RealSpacePrecision(), Agreement()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="compute everything locally; never contact W&B")
    a = ap.parse_args()

    res = offline_table()
    write_local(res)            # always first, so the slide has it even if logging fails
    print(f"wrote {HERE / 'comparison.md'} and comparison.csv")

    rows, st = load()
    import asyncio
    if a.dry_run:
        os.environ["WEAVE_DISABLED"] = "true"   # weave.init becomes a no-op: no network, nothing logged
        entity, project = "dry-run", "dry-run"
        import logging
        logging.getLogger("weave").setLevel(logging.CRITICAL)   # disabled client logs a failed-task line per call
    else:
        for k in ("WANDB_TEAM", "WANDB_PROJECT"):
            if not os.environ.get(k):
                sys.exit(f"{k} is not set (load WANDB_* env first)")
        entity, project = os.environ["WANDB_TEAM"], os.environ["WANDB_PROJECT"]
    weave, Model, scorers = build_weave()
    weave.init(f"{entity}/{project}")
    dataset = weave.Dataset(name="kerb-dashcam-human-labels", rows=rows)
    for disp, key in VARIANTS:
        ev = weave.Evaluation(name=disp, dataset=dataset, scorers=scorers)
        model = Model(name=disp, variant=disp, states=st[key])
        out = asyncio.run(ev.evaluate(model, __weave={"display_name": disp}))
        if a.dry_run:   # the Weave summaries must equal the offline table
            for kind, name in (("free", "FreePR"), ("occupied", "OccupiedPrecision"), ("real_space", "RealSpacePrecision")):
                for g in GROUPS:
                    o, r = out[name][g], res[disp][g][kind]
                    assert (o["tp"], o["system_said"], o["human_said"]) == (r["tp"], r["system_said"], r["human_said"]), (disp, g, kind)
            assert all(out["Agreement"][g]["exact"] == res[disp][g]["exact"] for g in GROUPS), disp
        print(f"done: {disp}")
    if a.dry_run:
        print(f"DRY RUN OK: {len(VARIANTS)} Weave Evaluations x {len(rows)} answers, matching comparison.csv; W&B not contacted")
    else:
        print(f"Weave evaluations: https://wandb.ai/{entity}/{project}/weave/evaluations")


if __name__ == "__main__":
    main()
