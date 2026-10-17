"""Kerb Map API and pages: pin/radius query, receipts, overrules, search, labelling.

Development, from the repo root:
    KERB_DB=local python -m query.api                 # or: uvicorn query.api:app --port 8000
Deployed (flat folder from query/build_deploy.py): python main.py, listening on 0.0.0.0:$PORT (8080).

The app is served behind a path prefix (/app) with the prefix stripped, so every route here is at
the root and every URL in the pages is relative.
"""

import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from common.kerb_schema import KEYS, SPACE_STATES
from query import store
from query.labels import router as labels_router
from query.replay import filter_as_of, router as replay_router
from query.sources import REPO, jsonl_dirs

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static") if os.path.isdir(os.path.join(HERE, "static")) else HERE
PAGES = {"index.html", "setup.html", "find.html", "find.js", "explore.html", "label.html", "board.html", "measured.html",
         "app.js", "api.js", "label.js", "style.css"}

app = FastAPI(title="Kerb Map API")
app.include_router(labels_router)
app.include_router(replay_router)


# ---------------------------------------------------------------- health

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/health")
def api_health():
    src = store.jsonl()
    src.refresh()
    d = store.db()
    counts = {t: len(d.read(t, None, limit=100000)) for t in KEYS}
    return {
        "ok": True,
        "backend": d.backend,
        "db_counts": counts,
        "jsonl_dirs": [os.path.relpath(p, REPO) if p.startswith(REPO) else p for p in jsonl_dirs()],
        "jsonl_files": len(src.files),
        "jsonl_counts": {t: len(rows) for t, rows in src.rows.items()},
        "jsonl_skipped_lines": src.skipped,
    }


# ---------------------------------------------------------------- query and receipts

def _check_state(state):
    if state and state not in SPACE_STATES:
        raise HTTPException(400, f"state must be one of {list(SPACE_STATES)}")


@app.get("/api/spaces")
def spaces(lat: Optional[float] = None, lon: Optional[float] = None, radius_m: float = 200.0,
           state: Optional[str] = None, side: Optional[str] = None, source: Optional[str] = None,
           limit: int = Query(500, le=5000), as_of: Optional[float] = None):
    """as_of (replay seconds): only receipts whose chunk has arrived in the running replay."""
    _check_state(state)
    rows = store.query_spaces(lat, lon, radius_m, state, side, source, 5000 if as_of is not None else limit)
    out = {"query": {"lat": lat, "lon": lon, "radius_m": radius_m, "state": state, "side": side,
                     "source": source, "as_of": as_of}}
    if as_of is not None:
        rows = filter_as_of(rows, as_of)[:limit]
        out["mode"] = "replay"
    return {"count": len(rows), **out, "spaces": [store.public(r) for r in rows]}


@app.get("/api/receipt/{space_id}")
def receipt(space_id: str, q: Optional[str] = None):
    space = store.find_space(space_id)
    if space is None:
        raise HTTPException(404, "no such space")
    trail = store.trail_for(space_id)
    overrules = sorted(store.read_all("kerb_overrules", {"space_id": space_id}), key=lambda r: r.get("at") or "")
    observations = sorted(store.read_all("kerb_observations", {"space_id": space_id}),
                          key=lambda r: (r.get("t_sec") is None, r.get("t_sec") or 0))
    camera = None
    if space.get("camera_id"):
        cams = store.read_all("kerb_cameras", {"camera_id": space["camera_id"]}, limit=1)
        camera = cams[0] if cams else None
    ev_uri, ev_start, ev_end = store.evidence_clip(space)
    out = {
        "space": store.public(space),
        "model_state": store.model_state(space, trail),
        "trail": trail,
        "overrules": overrules,
        "observations": observations,
        "camera": camera,
        "evidence_uri": ev_uri,
        "evidence_start": ev_start,
        "evidence_end": ev_end,
        "mark": store.mark(trail),
        "video": store.video_for(space),
    }
    if q:
        from query.search import explain
        out["query_explain"] = explain(q, space)
    return out


class OverruleIn(BaseModel):
    space_id: str
    new_state: str
    note: str = ""
    by: str = "anonymous"


@app.post("/api/overrule")
def overrule(body: OverruleIn):
    if body.new_state not in SPACE_STATES:
        raise HTTPException(400, f"new_state must be one of {list(SPACE_STATES)}")
    space = store.find_space(body.space_id)
    if space is None:
        raise HTTPException(404, "no such space")
    oid, before, after = store.write_overrule(space, body.new_state, body.note, body.by)
    return {"overrule_id": oid, "space_id": body.space_id,
            "before": {"state": before}, "after": {"state": after.get("state")},
            "space": store.public(after)}


class FeedbackIn(BaseModel):
    space_id: str
    vote: str
    reason: Optional[str] = None
    note: str = ""


# A driver's "not right" reason, and the state it implies (None: no state implied).
FEEDBACK_REASONS = {"taken": "occupied", "not_legal": "not_space", "driveway": "not_space", "other": None}


@app.post("/api/feedback")
def feedback(body: FeedbackIn):
    """A driver's thumbs up/down on a spot. Appended to kerb_overrules with by="user-feedback".

    It never changes the receipt (receipts_changed is empty); new_state is set only when the reason
    implies a state (taken -> occupied; not_legal or driveway -> not_space).
    """
    if body.vote not in ("up", "down"):
        raise HTTPException(400, "vote must be up or down")
    reason = (body.reason or "").strip() or None
    if body.vote == "up" and reason:
        raise HTTPException(400, "a reason goes with a down vote only")
    if reason is not None and reason not in FEEDBACK_REASONS:
        raise HTTPException(400, f"reason must be one of {list(FEEDBACK_REASONS)}")
    space = store.find_space(body.space_id)
    if space is None:
        raise HTTPException(404, "no such space")
    new_state = FEEDBACK_REASONS.get(reason) if reason else None
    note = f"vote={body.vote}" + (f" reason={reason}" if reason else "") + \
           (f": {body.note.strip()[:300]}" if body.note.strip() else "")
    oid, _, _ = store.write_overrule(space, new_state, note, "user-feedback", change_space=False)
    return {"feedback_id": oid, "space_id": body.space_id, "vote": body.vote, "reason": reason,
            "new_state": new_state, "receipt_changed": False}


@app.get("/api/search")
def search(q: str, lat: Optional[float] = None, lon: Optional[float] = None, radius_m: float = 500.0,
           state: Optional[str] = None, side: Optional[str] = None, source: Optional[str] = None,
           limit: int = Query(20, le=200)):
    from query import search as s
    _check_state(state)
    candidates = store.query_spaces(lat, lon, radius_m, state, side, source, limit=5000)
    ranked, embedder = s.rank(q, candidates)
    return {"q": q, "embedder": embedder, "filtered": len(candidates),
            "query_explain": s.describe_query(q, lat, lon, radius_m, state, side, source, embedder),
            "results": [{**store.public(r), "score": score} for score, r in ranked[:limit]]}


@app.get("/api/measured")
def measured():
    """Results table written by the measurement run (JSON). 404 until a run has produced one."""
    import json
    for path in (os.environ.get("KERB_MEASURED"), os.path.join(REPO, "results", "measured.json"),
                 os.path.join(HERE, "measured.json")):
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
    raise HTTPException(404, "not measured yet")


# ---------------------------------------------------------------- evidence (frames and clips)

def _allowed_dirs():
    dirs = [os.path.join(REPO, "dashcam", "out"), os.path.join(REPO, "streetcam", "out"),
            os.path.expanduser("~/kerb-data")] + jsonl_dirs()
    extra = os.environ.get("KERB_EVIDENCE_DIRS")
    if extra:
        dirs += [d for d in extra.split(os.pathsep) if d]
    return [os.path.realpath(d) for d in dirs]


@app.get("/api/evidence")
def evidence(uri: str, request: Request):
    """Pass an evidence frame or clip through to the browser. s3:// goes via VSS (or S3) server-side."""
    if uri.startswith(("http://", "https://")):
        return RedirectResponse(uri)
    if uri.startswith("s3://"):
        from query import evidence as ev
        try:
            resp = ev.open_s3(uri, request.headers.get("range"))
        except Exception as exc:
            raise HTTPException(502, f"could not read evidence ({exc})")
        headers = {k: resp.headers[k] for k in ("Content-Length", "Content-Range", "Accept-Ranges")
                   if resp.headers.get(k)}
        status = getattr(resp, "status", 200)

        def chunks():
            with resp:
                while True:
                    block = resp.read(256 * 1024)
                    if not block:
                        break
                    yield block
        return StreamingResponse(chunks(), status_code=status, headers=headers,
                                 media_type=resp.headers.get("Content-Type") or "application/octet-stream")
    path = uri[len("file://"):] if uri.startswith("file://") else uri
    if not os.path.isabs(path):
        path = os.path.join(REPO, path)
    real = os.path.realpath(path)
    if not any(real == d or real.startswith(d + os.sep) for d in _allowed_dirs()):
        raise HTTPException(403, "evidence path outside the allowed folders")
    if not os.path.isfile(real):
        raise HTTPException(404, "evidence file not found")
    return FileResponse(real)


# ---------------------------------------------------------------- pages (relative URLs only)

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/label")
def label_page():
    return FileResponse(os.path.join(STATIC, "label.html"))


@app.get("/{name}")
def page_file(name: str):
    if name not in PAGES:
        raise HTTPException(404, "not found")
    return FileResponse(os.path.join(STATIC, name))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
