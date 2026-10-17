"""Data access shared by the API, the labelling routes and the MCP tools.

Rows come from the database (common.kerbdb) and from the builders' JSONL fallback files
(query.sources). When both have the same key, the database row wins (that is where overrules land).
"""

import datetime as dt
import json
import uuid

from common import kerbdb
from common.kerb_schema import KEYS
from query.sources import JsonlSource

_db = None
_jsonl = None


def db():
    global _db
    if _db is None:
        _db = kerbdb.get_db()
        _db.ensure_tables()
    return _db


def jsonl():
    global _jsonl
    if _jsonl is None:
        _jsonl = JsonlSource()
    return _jsonl


def reset():
    """Forget cached connections (tests)."""
    global _db, _jsonl
    _db, _jsonl = None, None


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_test(row):
    return str(row.get("receipt_text") or "").startswith("TEST:")


def public(row):
    """A row as sent to the browser: no embedding vector, plus a TEST flag."""
    out = {k: v for k, v in row.items() if k != "embedding"}
    out["has_embedding"] = row.get("embedding") is not None
    out["is_test"] = is_test(row)
    return out


def merged(rows_db, rows_jsonl, key):
    out = {r[key]: r for r in rows_jsonl}
    out.update({r[key]: r for r in rows_db})
    return list(out.values())


def read_all(table, filters=None, limit=5000):
    return merged(db().read(table, filters, limit), jsonl().read(table, filters, limit), KEYS[table])


def find_space(space_id):
    rows = read_all("kerb_spaces", {"space_id": space_id}, limit=1)
    return rows[0] if rows else None


def query_spaces(lat, lon, radius_m, state=None, side=None, source=None, limit=500):
    """Spaces near a pin (both sources) or, without a pin, all spaces matching the filters."""
    if lat is None or lon is None:
        eq = {k: v for k, v in (("state", state), ("side", side), ("source", source)) if v}
        return read_all("kerb_spaces", eq, limit)[:limit]
    a = db().spaces_near(lat, lon, radius_m, state=state, side=side, source=source, limit=limit)
    b = jsonl().spaces_near(lat, lon, radius_m, state=state, side=side, source=source, limit=limit)
    rows = merged(a, b, "space_id")
    rows.sort(key=lambda r: r.get("distance_m", 0))
    return rows[:limit]


def trail_for(space_id):
    return sorted(read_all("kerb_trail", {"space_id": space_id}),
                  key=lambda r: (r.get("step_no") is None, r.get("step_no") or 0, r.get("at") or ""))


def model_state(space, trail):
    """The pipeline's own answer: the state before the first overrule, else the current state."""
    for step in trail:
        if step.get("step") == "overrule":
            try:
                return json.loads(step.get("detail_json") or "{}").get("before", space.get("state"))
            except ValueError:
                break
    return space.get("state")


def model_states(spaces):
    """model_state for many spaces with one trail read."""
    steps = sorted(read_all("kerb_trail", {"step": "overrule"}, limit=100000),
                   key=lambda r: (r.get("step_no") or 0, r.get("at") or ""))
    out = {sid: s.get("state") for sid, s in spaces.items()}
    done = set()
    for step in steps:
        sid = step["space_id"]
        if sid in spaces and sid not in done:
            done.add(sid)
            out[sid] = model_state(spaces[sid], [step])
    return out


def mark(trail):
    """Box or point to draw on the evidence frame, from the trail details ("box" or "point" keys)."""
    for step in trail:
        try:
            detail = json.loads(step.get("detail_json") or "{}")
        except ValueError:
            continue
        if isinstance(detail, dict):
            if isinstance(detail.get("box"), list) and len(detail["box"]) == 4:
                return {"box": detail["box"], "frame_size": detail.get("frame_size")}
            if isinstance(detail.get("point"), list) and len(detail["point"]) == 2:
                return {"point": detail["point"], "frame_size": detail.get("frame_size")}
    return None


def video_for(space):
    """Clip to play for a receipt: original_video around t_sec (seconds into that clip)."""
    uri = space.get("original_video")
    if not uri:
        return None
    t = space.get("t_sec")
    if t is None:
        return {"uri": uri, "start": None, "end": None}
    return {"uri": uri, "start": max(0.0, float(t) - 2.0), "end": float(t) + 3.0}


VIDEO_EXT = (".mp4", ".mov", ".webm", ".mkv")
SEGMENT_S = 5.0


def evidence_clip(space):
    """(uri, start, end): the evidence file and, for a video, the seconds to play within that file.

    uri is evidence_uri, else original_video. t_sec is read as seconds into original_video; for a
    5 s segment file (name contains "_segment_") it is shifted by (segment_number - 1) * 5 s.
    """
    uri = space.get("evidence_uri") or space.get("original_video")
    t = space.get("t_sec")
    if not uri or t is None or not uri.lower().split("?")[0].endswith(VIDEO_EXT):
        return uri, None, None
    t = float(t)
    if uri == space.get("original_video"):
        return uri, max(0.0, t - 2.0), t + 3.0
    if "_segment_" in uri and space.get("segment_number"):
        local = t - (int(space["segment_number"]) - 1) * SEGMENT_S
        if 0 <= local <= SEGMENT_S:
            return uri, max(0.0, local - 2.0), min(SEGMENT_S, local + 3.0)
    return uri, None, None


def write_overrule(space, new_state, note, by, change_space=True):
    """Record an overrule. With change_space the receipt's state is set and an overrule trail step added."""
    at = now_iso()
    before = space.get("state")
    oid = f"ovr_{uuid.uuid4().hex[:12]}"
    d = db()
    d.insert("kerb_overrules", [{
        "overrule_id": oid, "space_id": space["space_id"], "by": by, "new_state": new_state, "note": note,
        "at": at, "receipts_changed": [space["space_id"]] if change_space else [],
    }])
    after = space
    if change_space:
        trail = trail_for(space["space_id"])
        step_no = max([t.get("step_no") or 0 for t in trail] + [0]) + 1
        d.insert("kerb_trail", [{
            "trail_id": f"{space['space_id']}_ovr_{uuid.uuid4().hex[:8]}", "space_id": space["space_id"],
            "step_no": step_no, "step": "overrule",
            "detail_json": json.dumps({"before": before, "after": new_state, "by": by, "note": note,
                                       "overrule_id": oid}),
            "at": at,
        }])
        # Copy the full row so a space that only lived in JSONL now exists in the database too.
        after = d.upsert_space({**space, "state": new_state})
    return oid, before, after
