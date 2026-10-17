"""Live replay: release a drive's existing receipts chunk by chunk, as if each 30 s clip had just synced.

This only replays receipts already produced from the supplied footage. Nothing is ingested or recomputed.
Every response carries "mode": "replay".

Timeline source (first found): replay_chunks.csv next to this file (written by build_deploy.py),
$KERB_VIDEO_GEO, <repo>/data/video_geo.csv, <repo>/data/pie_map.csv. A drive is one camera, and for
the dashcam one PIE set (set01..set06), e.g. "pie_cam-3:set01" or "neighborhood_cam-1". Chunks are taken
in file-name order (the names start with the clip time). Chunk i "arrives" at virtual time (i + 1) * 30 s.
"""

import os
import threading
import time
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from query import store
from query.drivelog import summarize
from query.sources import REPO
from query.timeline import load_chunks

CHUNK_S = 30.0
NOTE = "replay of receipts already produced from the supplied footage; no new ingestion"
HERE = os.path.dirname(os.path.abspath(__file__))

router = APIRouter()
_lock = threading.Lock()
_state = {"run": None}
_timeline_cache = {"path": None, "mtime": None, "drives": None}


def _timeline_path():
    for path in (os.path.join(HERE, "replay_chunks.csv"), os.environ.get("KERB_VIDEO_GEO"),
                 os.path.join(REPO, "data", "video_geo.csv"), os.path.join(REPO, "data", "pie_map.csv")):
        if path and os.path.isfile(path):
            return path
    return None


def drives():
    path = _timeline_path()
    if path is None:
        return {}
    mtime = os.path.getmtime(path)
    if _timeline_cache["path"] != path or _timeline_cache["mtime"] != mtime:
        d = defaultdict(list)
        for drive, _name, video in load_chunks(path):
            d[drive].append(video)
        _timeline_cache.update(path=path, mtime=mtime, drives=dict(d))
    return _timeline_cache["drives"]


def _status(run, now=None):
    now = now or time.time()
    as_of = (now - run["started"]) * run["speed"]
    arrived = min(len(run["chunks"]), int(as_of // CHUNK_S))
    done = arrived >= len(run["chunks"])
    return {"mode": "replay", "note": NOTE, "replay_id": run["id"], "drive_id": run["drive"],
            "speed": run["speed"], "as_of": round(as_of, 1), "clips_total": len(run["chunks"]),
            "clips_done": arrived, "current_clip": run["chunks"][arrived - 1] if arrived else None,
            "active": not done, "done": done, "started_at": run["started_iso"]}


def arrived_videos(as_of: float):
    """original_video -> arrival time for chunks of the active replay that have arrived by `as_of`."""
    run = _state["run"]
    if run is None:
        return None
    out = {}
    for i, video in enumerate(run["chunks"]):
        t = (i + 1) * CHUNK_S
        if t <= as_of:
            out[video] = t
    return out


def filter_as_of(rows, as_of):
    """Keep only receipts whose chunk has arrived in the active replay (empty if no replay is running)."""
    arrived = arrived_videos(as_of)
    if not arrived:
        return []
    return [{**r, "replay_arrived_s": arrived[r["original_video"]]} for r in rows
            if r.get("original_video") in arrived]


class StartIn(BaseModel):
    drive_id: Optional[str] = None   # e.g. "pie_cam-3:set01"
    drive: Optional[str] = None      # same as drive_id
    camera_id: Optional[str] = None  # street camera, e.g. "neighborhood_cam-1"
    set: Optional[str] = None        # dashcam set, e.g. "set01" (camera pie_cam-3)
    speed: float = 1.0


@router.get("/api/replay/drives")
def list_drives():
    d = drives()
    spaces = store.read_all("kerb_spaces", None, limit=1000000)
    per_video = defaultdict(int)
    for s in spaces:
        per_video[s.get("original_video")] += 1
    out = []
    for k, v in sorted(d.items()):
        n = sum(per_video[x] for x in v)
        out.append({"drive_id": k, "label": f"{k} ({n} receipts)", "clips": len(v), "receipts": n})
    return {"mode": "replay", "note": NOTE, "timeline": os.path.basename(_timeline_path() or "") or None,
            "drives": out}


@router.get("/api/drives/{drive_id:path}")
def drive_log(drive_id: str):
    """Streets and states this drive's existing receipts already recorded. No writes."""
    d = drives()
    if drive_id not in d:
        raise HTTPException(404, f"unknown drive {drive_id!r}; see api/replay/drives")
    spaces = store.read_all("kerb_spaces", None, limit=1000000)
    return summarize(drive_id, d[drive_id], spaces)


@router.post("/api/replay/start")
def start(body: StartIn):
    if body.speed <= 0 or body.speed > 100:
        raise HTTPException(400, "speed must be in (0, 100]")
    drive = body.drive_id or body.drive or (f"pie_cam-3:{body.set}" if body.set else body.camera_id)
    d = drives()
    if not drive or drive not in d:
        raise HTTPException(404, f"unknown drive {drive!r}; see api/replay/drives")
    with _lock:
        _state["run"] = {"id": f"r_{int(time.time() * 1000)}", "drive": drive, "speed": float(body.speed),
                         "chunks": d[drive],
                         "started": time.time(), "started_iso": store.now_iso()}
        return _status(_state["run"])


@router.get("/api/replay/status")
def status(replay_id: Optional[str] = None):
    """Progress plus the receipts released so far (rows of kerb_spaces, no embeddings)."""
    run = _state["run"]
    if run is None or (replay_id and replay_id != run["id"]):
        return {"mode": "replay", "note": NOTE, "running": False, "active": False, "spaces": []}
    st = _status(run)
    rows = filter_as_of(store.read_all("kerb_spaces", None, limit=1000000), st["as_of"])
    rows.sort(key=lambda r: (r["replay_arrived_s"], r.get("t_sec") or 0))
    st["clip_time"] = max((r["last_seen"] for r in rows if r.get("last_seen")), default=None)
    return {**st, "running": True, "spaces": [store.public(r) for r in rows]}


class StopIn(BaseModel):
    replay_id: Optional[str] = None


@router.post("/api/replay/stop")
def stop(body: Optional[StopIn] = None):
    with _lock:
        _state["run"] = None
    return {"mode": "replay", "running": False}
