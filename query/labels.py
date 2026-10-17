"""Batch labelling: several people label kerb items in parallel without overwriting each other.

- An item is a kerb_spaces row (dashcam or street camera); item_id = space_id.
- Split: each item is dev or held-out by a fixed hash of its id (about 1 in 5 held-out). Held-out
  items are claimed only with split="heldout" and are left out of the summary used for tuning.
- Overlap: about 1 in 10 items (fixed hash) needs two different labellers, to measure agreement.
  Items where two people disagree need a third look and are served first.
- Claims: a batch of 20 items, locked to one person for 15 minutes; locks expire on their own.
  Batch locks live in this process's memory (the app runs as one replica); labels are durable rows.
- Labels are append-only rows in kerb_eval_cases. case_id = <item_id>__<labeller>__<UTC time>, so
  a re-label by the same person supersedes their earlier one (latest wins), and nothing is overwritten.
  A label that disagrees with the pipeline also writes an overrule that does not change the receipt.
"""

import csv
import hashlib
import io
import json
import os
import threading
import time
import uuid
import datetime as dt
from collections import Counter, defaultdict
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from common.kerb_schema import SPACE_STATES, SchemaError
from query import store

BATCH_SIZE = 20
LOCK_SECONDS = 15 * 60
LOW_CONFIDENCE = 0.6
NOT_SPACE_REASONS = ("driveway", "junction", "crossing", "hydrant", "bus_stop", "travel_lane", "too_short", "other")

router = APIRouter()
_lock = threading.Lock()
_batches = {}  # batch_id -> {"id", "labeller", "split", "items", "expires", "released"}


def _h(text, mod):
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) % mod


def split_of(item_id):
    return "heldout" if _h("split:" + item_id, 5) == 0 else "dev"


def is_overlap(item_id):
    return _h("overlap:" + item_id, 10) == 0


def base_label(label):
    return (label or "").split("/", 1)[0]


def clean_name(name):
    return (name or "").strip().replace("__", "_")


def _stamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def parse_case(case):
    item_id, labeller, stamp = case["case_id"].rsplit("__", 2)
    return item_id, labeller, stamp


def latest_labels():
    """{item_id: {labeller: case}} keeping each person's most recent label per item."""
    out = defaultdict(dict)
    for c in store.db().read("kerb_eval_cases", None, limit=1000000):
        try:
            item, who, stamp = parse_case(c)
        except ValueError:
            continue
        prev = out[item].get(who)
        if prev is None or parse_case(prev)[2] < stamp:
            out[item][who] = {**c, "_stamp": stamp}
    return out


def disputed(labels_for_item):
    return len({base_label(c["label"]) for c in labels_for_item.values()}) > 1


def needed(item_id, labels_for_item):
    if disputed(labels_for_item):
        return 3
    return 2 if is_overlap(item_id) else 1


def _active(now=None):
    now = now or time.time()
    return [b for b in _batches.values() if not b["released"] and b["expires"] > now]


def _priority(space, labels_for_item):
    if disputed(labels_for_item) and len(labels_for_item) < 3:
        tier = 0
    elif space.get("state") == "cannot_tell" or (space.get("confidence") is not None
                                                 and space["confidence"] < LOW_CONFIDENCE):
        tier = 1
    elif space.get("state") == "not_space":
        tier = 2  # gaps the rules rejected
    else:
        tier = 3
    return (tier, _h("order:" + space["space_id"], 1000003))


def _item(space, trail=None):
    trail = trail if trail is not None else store.trail_for(space["space_id"])
    uri, start, end = store.evidence_clip(space)
    return {"item_id": space["space_id"], "space_id": space["space_id"], "space": store.public(space),
            "clip_start": start, "clip_end": end, "street": space.get("street"),
            "side": space.get("side"), "source": space.get("source"),
            "model_state": store.model_state(space, trail), "evidence_uri": uri,
            "mark": store.mark(trail), "trail": trail, "video": store.video_for(space),
            "split": split_of(space["space_id"]),
            "overlap": is_overlap(space["space_id"])}


# ---------------------------------------------------------------- claim / release

class ClaimIn(BaseModel):
    labeller: str
    split: Optional[str] = None   # dev (default) or heldout
    kind: Optional[str] = None    # "third_look": only items where two people disagree
    include_test: bool = True


@router.post("/api/batches/claim")
def claim(body: ClaimIn):
    who = clean_name(body.labeller)
    if not who:
        raise HTTPException(400, "labeller name required")
    split = body.split or "dev"
    if split not in ("dev", "heldout"):
        raise HTTPException(400, "split must be dev or heldout")
    third_only = body.kind == "third_look"
    labels = latest_labels()
    spaces = {s["space_id"]: s for s in store.read_all("kerb_spaces", None, limit=1000000)}
    with _lock:
        now = time.time()
        active = _active(now)
        # Hand back the person's own open batch if it still has unlabelled items.
        for b in active:
            if b["labeller"] == who and b["split"] == split and not third_only:
                left = [i for i in b["items"] if who not in labels.get(i, {})]
                if left:
                    return _batch_view(b, labels, spaces)
                b["released"] = True
        holders = defaultdict(set)
        for b in _active(now):
            for i in b["items"]:
                holders[i].add(b["labeller"])
        pool = []
        for sid, s in spaces.items():
            if split_of(sid) != split or (not body.include_test and store.is_test(s)):
                continue
            done = labels.get(sid, {})
            if third_only and not (disputed(done) and len(done) < 3):
                continue
            if who in done or who in holders[sid]:
                continue
            if len(done) + len(holders[sid]) >= needed(sid, done):
                continue
            pool.append(s)
        pool.sort(key=lambda s: _priority(s, labels.get(s["space_id"], {})))
        items = [s["space_id"] for s in pool[:BATCH_SIZE]]
        if not items:
            return {"batch_id": None, "items": [], "remaining": 0, "reasons": list(NOT_SPACE_REASONS)}
        b = {"id": f"b_{uuid.uuid4().hex[:10]}", "labeller": who, "split": split, "items": items,
             "expires": now + LOCK_SECONDS, "released": False}
        _batches[b["id"]] = b
        view = _batch_view(b, labels, spaces)
        view["remaining"] = len(pool) - len(items)
        return view


def _batch_view(b, labels, spaces):
    items = []
    for i in b["items"]:
        if i in spaces:
            items.append({**_item(spaces[i]), "done": b["labeller"] in labels.get(i, {})})
    return {"batch_id": b["id"], "labeller": b["labeller"], "split": b["split"],
            "expires_in_s": max(0, int(b["expires"] - time.time())), "items": items,
            "reasons": list(NOT_SPACE_REASONS)}


@router.post("/api/batches/{batch_id}/release")
def release(batch_id: str):
    with _lock:
        b = _batches.get(batch_id)
        if b is None:
            raise HTTPException(404, "no such batch")
        b["released"] = True
    return {"batch_id": batch_id, "released": True}


# ---------------------------------------------------------------- labels

class LabelIn(BaseModel):
    item_id: str
    labeller: str
    label: str
    reason: str = ""
    note: str = ""
    ms_taken: Optional[int] = None


def _notes_path():
    return os.environ.get("KERB_LABEL_NOTES") or os.path.expanduser("~/kerb-data/label_notes.jsonl")


@router.post("/api/labels")
def add_label(body: LabelIn):
    who = clean_name(body.labeller)
    if not who:
        raise HTTPException(400, "labeller name required")
    if body.label not in SPACE_STATES:
        raise HTTPException(400, f"label must be one of {list(SPACE_STATES)}")
    if body.label == "not_space" and body.reason and body.reason not in NOT_SPACE_REASONS:
        raise HTTPException(400, f"reason must be one of {list(NOT_SPACE_REASONS)}")
    space = store.find_space(body.item_id)
    if space is None:
        raise HTTPException(404, "no such item")
    trail = store.trail_for(body.item_id)
    m = store.mark(trail)
    x = y = None
    if m and "point" in m:
        x, y = float(m["point"][0]), float(m["point"][1])
    elif m:
        x, y = (m["box"][0] + m["box"][2]) / 2.0, (m["box"][1] + m["box"][3]) / 2.0
    label = f"not_space/{body.reason}" if body.label == "not_space" and body.reason else body.label
    case = {"case_id": f"{body.item_id}__{who}__{_stamp()}", "source": space.get("source"),
            "original_video": space.get("original_video"), "t_sec": space.get("t_sec"), "x": x, "y": y,
            "label": label, "labeller": who, "split": split_of(body.item_id)}
    case["frozen_sha256"] = hashlib.sha256(json.dumps(case, sort_keys=True).encode()).hexdigest()
    try:
        store.db().insert("kerb_eval_cases", [case])
    except SchemaError as exc:
        raise HTTPException(400, str(exc))
    mstate = store.model_state(space, trail)
    agrees = mstate == body.label
    overrule_id = None
    note = body.note.strip()
    if not agrees:
        overrule_id, _, _ = store.write_overrule(
            space, body.label, f"label {label} (case {case['case_id']})" + (f": {note}" if note else ""),
            f"labeller:{who}", change_space=False)
    if note or body.ms_taken is not None:
        try:
            with open(_notes_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"case_id": case["case_id"], "item_id": body.item_id, "labeller": who,
                                     "note": note, "ms_taken": body.ms_taken, "at": store.now_iso()}) + "\n")
        except OSError:
            pass
    return {"label_id": case["case_id"], "case_id": case["case_id"], "split": case["split"],
            "model_state": mstate, "agrees": agrees,
            "overrule_id": overrule_id}


# ---------------------------------------------------------------- board, summary, export

def _stamp_time(stamp):
    return dt.datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=dt.timezone.utc).timestamp()


@router.get("/api/board")
def board():
    labels = latest_labels()
    spaces = {s["space_id"]: s for s in store.read_all("kerb_spaces", None, limit=1000000)}
    now = time.time()
    per = defaultdict(lambda: {"done": 0, "in_progress": 0, "last_10_min": 0, "overlap_compared": 0,
                               "overlap_agree": 0})
    for item, by in labels.items():
        if item not in spaces:
            continue
        for who, c in by.items():
            p = per[who]
            p["done"] += 1
            if now - _stamp_time(c["_stamp"]) <= 600:
                p["last_10_min"] += 1
            others = [o for w, o in by.items() if w != who]
            if others:
                p["overlap_compared"] += 1
                if all(base_label(o["label"]) == base_label(c["label"]) for o in others):
                    p["overlap_agree"] += 1
    with _lock:
        for b in _active(now):
            per[b["labeller"]]["in_progress"] += sum(1 for i in b["items"] if b["labeller"] not in labels.get(i, {}))
    people = []
    for who, p in sorted(per.items(), key=lambda kv: -kv[1]["done"]):
        people.append({"labeller": who, "done": p["done"], "in_progress": p["in_progress"],
                       "items_per_min": round(p["last_10_min"] / 10.0, 2), "agree": p["overlap_agree"],
                       "compared": p["overlap_compared"],
                       "agreement": round(p["overlap_agree"] / p["overlap_compared"], 3)
                       if p["overlap_compared"] else None})
    progress = {}
    for split in ("dev", "heldout"):
        ids = [i for i in spaces if split_of(i) == split]
        progress[split] = {"done": sum(1 for i in ids if labels.get(i)), "total": len(ids)}
    third = [{"item_id": i, "labels": {w: c["label"] for w, c in by.items()}}
             for i, by in labels.items() if i in spaces and disputed(by) and len(by) < 3]
    failures = summary()["top_failures"]
    return {"labellers": people, "progress": progress, "needs_third_look": len(third),
            "needs_third_look_items": third,
            "disagreements": [{"reason": f["what"], "count": f["count"]} for f in failures],
            "agreement_basis": "items labelled by two or more people"}


def _consensus(by):
    counts = Counter(base_label(c["label"]) for c in by.values())
    top = counts.most_common(2)
    if len(top) > 1 and top[0][1] == top[1][1]:
        return None  # tie: no consensus yet
    winner = top[0][0]
    full = Counter(c["label"] for c in by.values() if base_label(c["label"]) == winner).most_common(1)[0][0]
    return winner, full


@router.get("/api/labels/summary")
def summary():
    """Human vs pipeline on dev items only (held-out stays untouched for the final run)."""
    labels = latest_labels()
    spaces = {s["space_id"]: s for s in store.read_all("kerb_spaces", None, limit=1000000)}
    model = store.model_states(spaces)
    confusion = defaultdict(lambda: defaultdict(int))
    failures = Counter()
    disagree = []
    no_consensus = 0
    items = 0
    for item, by in labels.items():
        if item not in spaces or split_of(item) != "dev" or not by:
            continue
        cons = _consensus(by)
        if cons is None:
            no_consensus += 1
            continue
        items += 1
        human, full = cons
        confusion[model[item]][human] += 1
        if human != model[item]:
            failures[f"{full} called {model[item]}"] += 1
            s = spaces[item]
            disagree.append({"item_id": item, "model_state": model[item], "human": full,
                             "labels": {w: c["label"] for w, c in by.items()}, "confidence": s.get("confidence"),
                             "source": s.get("source"), "street": s.get("street"),
                             "evidence_uri": s.get("evidence_uri"), "is_test": store.is_test(s)})
    by_labeller = defaultdict(lambda: {"labels": 0, "agree": 0, "compared": 0})
    total = 0
    for item, by in labels.items():
        for who, c in by.items():
            total += 1
            p = by_labeller[who]
            p["labels"] += 1
            if item in spaces and split_of(item) == "dev":
                p["compared"] += 1
                p["agree"] += int(base_label(c["label"]) == model[item])
    agree = sum(confusion[s][s] for s in confusion)
    return {"split": "dev", "total_labels": total, "by_labeller": dict(by_labeller),
            "by_labeller_basis": "agreement with the pipeline on dev items", "items_compared": items, "agree": agree,
            "agreement": round(agree / items, 3) if items else None, "no_consensus": no_consensus,
            "confusion_model_by_human": {m: dict(h) for m, h in confusion.items()},
            "top_failures": [{"what": k, "count": v} for k, v in failures.most_common(5)],
            "disagreements": disagree}


@router.get("/api/labels/export")
def export(split: str = "dev", format: str = "json"):
    if split not in ("dev", "heldout"):
        raise HTTPException(400, "split must be dev or heldout")
    labels = latest_labels()
    spaces = {s["space_id"]: s for s in store.read_all("kerb_spaces", None, limit=1000000)}
    model = store.model_states(spaces)
    rows = []
    for item in sorted(labels):
        if split_of(item) != split:
            continue
        for who in sorted(labels[item]):
            c = labels[item][who]
            rows.append({"item_id": item, "labeller": who, "label": c["label"], "case_id": c["case_id"],
                         "case_sha256": c.get("frozen_sha256"), "model_state": model.get(item),
                         "source": c.get("source"), "original_video": c.get("original_video"),
                         "t_sec": c.get("t_sec"), "x": c.get("x"), "y": c.get("y"), "split": split})
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    if format == "csv":
        buf = io.StringIO()
        cols = list(rows[0]) if rows else ["item_id", "labeller", "label"]
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"X-Export-SHA256": digest,
                                 "Content-Disposition": f'attachment; filename="labels_{split}_{digest[:12]}.csv"'})
    return {"split": split, "count": len(rows), "frozen_sha256": digest, "frozen_at": store.now_iso(),
            "rows": rows}


@router.get("/api/labels/reasons")
def reasons():
    return {"not_space_reasons": list(NOT_SPACE_REASONS)}
