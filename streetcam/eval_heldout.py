#!/usr/bin/env python3
"""Score neighborhood_cam-1 track states against human labels.

Two modes:

  Held-out (default): scores every returned CSV in <labels-dir>/returned/
  except batches A3 and A6, which were seen while the occupancy fix was made.
  Items that also appear in A3/A6 (the overlap slots) are dropped too.
  --glob limits it to some files, e.g. --glob "answers_A2_*.csv".
      python streetcam/eval_heldout.py --labels-dir <path>/day-of/label-batches
  writes streetcam/out/eval_heldout.json

  Dev (--dev): scores <labels-dir>/merged_labels.csv (batches A3 + A6 only).
      python streetcam/eval_heldout.py --dev --labels-dir <path>/day-of/label-batches
  writes streetcam/out/eval_labels.json

"before" is the system_state column of the label CSV (what the labeller was
shown, from the old per-track still test). "after" is the state in
streetcam/out/track_states.jsonl. Items labelled cant_tell or skipped, or
with no human label, are left out of agreement and listed as excluded.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEV_BATCHES = {"A3", "A6"}
STATES = ["parked", "traffic", "not_space"]


def load_states(path: Path):
    out = {}
    with path.open() as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                out[r["track_id"]] = r
    return out


def read_csv(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def latest_per_item(rows):
    """Keep the latest answer per item (same rule as merge_answers.py)."""
    best = {}
    for r in rows:
        key = r["item_id"]
        if key not in best or (r.get("answered_at") or "") >= (best[key].get("answered_at") or ""):
            best[key] = r
    return list(best.values())


def confusion(pairs):
    table = {s: {h: 0 for h in STATES} for s in STATES}
    for sys_state, human in pairs:
        if sys_state in table and human in table[sys_state]:
            table[sys_state][human] += 1
    return table


def score(rows, states):
    scored, excluded, wrong = [], [], []
    for r in sorted(rows, key=lambda r: r["item_id"]):
        human = (r.get("human_label") or "").strip()
        if human not in STATES:
            excluded.append({"item_id": r["item_id"], "human_label": human or None})
            continue
        st = states.get(r["track_id"])
        after = st["state"] if st else None
        before = r.get("system_state")
        scored.append((r, before, after, human))
        if after != human:
            wrong.append({
                "item_id": r["item_id"],
                "batch": r.get("batch"),
                "track_id": r["track_id"],
                "t_start": r.get("t_start"),
                "t_end": r.get("t_end"),
                "human_label": human,
                "reason": r.get("reason") or None,
                "before": before,
                "after": after,
                "after_area": st.get("area") if st else None,
                "after_why": st.get("why") if st else None,
            })
    n = len(scored)
    agree_before = sum(1 for _, b, _, h in scored if b == h)
    agree_after = sum(1 for _, _, a, h in scored if a == h)
    changed = Counter(f"{b} -> {a}" for _, b, a, _ in scored if a != b)
    fixed = sum(1 for _, b, a, h in scored if b != h and a == h)
    broken = sum(1 for _, b, a, h in scored if b == h and a != h)
    return {
        "n_items_scored": n,
        "n_excluded": len(excluded),
        "excluded": excluded,
        "agree_before": agree_before,
        "agree_after": agree_after,
        "fixed_by_change": fixed,
        "broken_by_change": broken,
        "state_changes": dict(changed),
        "confusion_before_rows_system_cols_human": confusion((b, h) for _, b, _, h in scored),
        "confusion_after_rows_system_cols_human": confusion((a, h) for _, _, a, h in scored),
        "human_label_counts": dict(Counter(h for *_, h in scored)),
        "batches": sorted({r.get("batch") or "?" for r, *_ in scored}),
        "still_wrong_after": wrong,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--labels-dir", type=Path, required=True,
                   help="day-of/label-batches folder (has merged_labels.csv and returned/)")
    p.add_argument("--states", type=Path, default=HERE / "out" / "track_states.jsonl")
    p.add_argument("--dev", action="store_true", help="score merged_labels.csv (A3+A6, seen during the fix)")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--glob", default="*.csv", help="which returned CSVs to score, e.g. 'answers_A2_*.csv'")
    args = p.parse_args()
    states = load_states(args.states)

    if args.dev:
        src = args.labels_dir / "merged_labels.csv"
        rows = [r for r in read_csv(src) if r.get("camera_id", "neighborhood_cam-1") == "neighborhood_cam-1"]
        other = sorted({r.get("batch") for r in rows} - DEV_BATCHES)
        if other:
            raise SystemExit(f"merged_labels.csv has batches {other}; dev mode expects A3/A6 only")
        result = {
            "split": "dev",
            "note": ("Batches A3 and A6 were looked at while the polygons and occupancy rule were changed. "
                     "This is a development score, not a held-out one. Batches A1, A2, A4, A5 returned later "
                     "are the held-out check: run this script without --dev."),
            "source": [src.name],
            **score(latest_per_item(rows), states),
        }
        out = args.out or HERE / "out" / "eval_labels.json"
    else:
        files = sorted((args.labels_dir / "returned").glob(args.glob))
        # items that also sit in a dev batch (overlap slots) were seen during the fix
        dev_items = set()
        for f in sorted((args.labels_dir / "returned").glob("*.csv")):
            dev_items |= {r["item_id"] for r in read_csv(f) if r.get("batch") in DEV_BATCHES}
        rows, used, skipped = [], [], []
        for f in files:
            part = [r for r in read_csv(f) if r.get("batch") not in DEV_BATCHES
                    and r.get("camera_id", "neighborhood_cam-1") == "neighborhood_cam-1"]
            (used if part else skipped).append(f.name)
            rows.extend(part)
        seen_in_dev = sorted({r["item_id"] for r in rows if r["item_id"] in dev_items})
        rows = [r for r in rows if r["item_id"] not in dev_items]
        if not rows:
            raise SystemExit("no held-out answers found (only A3/A6 or nothing in returned/)")
        result = {
            "split": "held-out",
            "note": "Batches other than A3/A6; not looked at while making the occupancy fix.",
            "source": used,
            "files_skipped_dev_only": skipped,
            "items_dropped_also_in_dev_batches": seen_in_dev,
            **score(latest_per_item(rows), states),
        }
        out = args.out or HERE / "out" / "eval_heldout.json"

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: result[k] for k in ("split", "batches", "n_items_scored", "agree_before", "agree_after",
                                             "fixed_by_change", "broken_by_change")}))


if __name__ == "__main__":
    main()
