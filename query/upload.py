"""Upload a dashcam clip to VAST, wait until VSS has indexed it, run the kerb pipeline, save receipts.

    POST api/upload            multipart: file (video), gps (optional CSV), sample_id (optional) -> {job_id}
    GET  api/upload/samples    prepared demo samples whose GPS the server knows
    GET  api/upload/{job_id}   job state, steps, counts, map centre

A job runs in a background thread:
  1 Upload to VAST                   POST {VSS_URL}/api/v1/videos/upload (server-side login, see evidence.py)
  2 Indexed by VAST (YOLO + Cosmos)  poll GET /api/v1/videos/explore?scope=mine until the new parent and all its
                                     segments are indexed (KERB_UPLOAD_TIMEOUT_S, default 360 s)
  3 Kerb analysis                    GET /api/v1/videos/detections per segment, then kerbpipe.run
  4 Saved to VastDB                  kerb_spaces / kerb_observations / kerb_trail via kerbdb (space_id up_<job_id>_...)

Jobs live in memory and in a small JSON file (KERB_UPLOAD_JOBS, default ~/kerb-data/upload_jobs.json). A job id that
is in neither (e.g. after a restart) is rebuilt from its saved receipts, so a finished job keeps answering.
"""

import datetime as dt
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.parser import BytesParser
from email.policy import HTTP

from fastapi import APIRouter, HTTPException, Request

from query import evidence, kerbpipe, store

router = APIRouter()
HERE = os.path.dirname(os.path.abspath(__file__))

STEPS = ("Upload to VAST", "Indexed by VAST (YOLO + Cosmos)", "Kerb analysis", "Saved to VastDB")
CAMERA_ID = os.environ.get("KERB_UPLOAD_CAMERA", "dashcam-demo")
INGEST_PROMPT = (
    "Describe the kerbside of the street in this clip for a parking map. Write exactly these lines:\n"
    "KERB_LEFT: number of parked vehicles; empty kerb gap longer than one car length (yes/no, about how many metres); "
    "markings or features at the kerb (yellow lines, parking bays, driveway, hydrant, bus stop, crossing, junction).\n"
    "KERB_RIGHT: same fields as KERB_LEFT.\n"
    "STOPPED_IN_LANE: vehicles stopped in a travel lane, for example queued at a light (yes/no, how many).\n"
    "PARKING_SIGNS: text of any parking or no-parking sign that is readable, else none.\n"
    "CAMERA: moving dashcam or fixed camera.\n"
    "SCENE: one sentence on street type, time of day and weather.\n"
    "Do not describe faces and do not read number plates.")[:800]

_lock = threading.Lock()
_jobs = {}


def _now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _settings():
    return {"poll_s": float(os.environ.get("KERB_UPLOAD_POLL_S", "5")),
            "timeout_s": float(os.environ.get("KERB_UPLOAD_TIMEOUT_S", "360")),
            "capture_type": os.environ.get("KERB_UPLOAD_CAPTURE_TYPE", "streets"),
            "location": os.environ.get("KERB_UPLOAD_LOCATION", "toronto"),
            "tags": os.environ.get("KERB_UPLOAD_TAGS", "kerbmap-demo")}


# ---------------------------------------------------------------- jobs file

def _jobs_path():
    return os.environ.get("KERB_UPLOAD_JOBS") or os.path.join(os.path.expanduser("~"), "kerb-data", "upload_jobs.json")


def _save():
    path = _jobs_path()
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with _lock:
            data = json.dumps({k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                               for k, v in _jobs.items()})
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError:
        pass  # the in-memory state still answers


def _load_file():
    try:
        with open(_jobs_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def new_job(filename, track_source):
    jid = uuid.uuid4().hex[:10]
    job = {"job_id": jid, "state": "uploading", "filename": filename, "object_key": None,
           "steps": [{"name": n, "status": "pending", "at": None, "detail": ""} for n in STEPS],
           "segments_found": 0, "receipts_by_state": {s: 0 for s in kerbpipe.STATES}, "center": None,
           "message": "Uploading to VAST", "gps": track_source, "created": _now()}
    with _lock:
        _jobs[jid] = job
    _save()
    return job


def _step(job, i, status, detail=None):
    with _lock:
        st = job["steps"][i]
        st["status"] = status
        st["at"] = _now()
        if detail is not None:
            st["detail"] = detail
    _save()


def _set(job, **kw):
    with _lock:
        job.update(kw)
    _save()


def _fail(job, message):
    with _lock:
        for st in job["steps"]:
            if st["status"] == "running":
                st["status"], st["at"], st["detail"] = "error", _now(), message
        job["state"], job["message"] = "error", message
    _save()


# ---------------------------------------------------------------- VSS calls (server-side login)

def _vss(path, params=None, data=None, headers=None, timeout=60):
    url = evidence.vss_settings()[0]
    if not url:
        raise RuntimeError("VSS_URL is not set")
    full = url + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in (0, 1):
        token = evidence._vss_token(force=attempt == 1)
        req = urllib.request.Request(full, data=data, headers={**(headers or {}), "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=evidence._CTX) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code != 401 or attempt == 1:
                body = exc.read()[:300].decode("utf-8", "replace")
                raise RuntimeError(f"VSS {path} answered HTTP {exc.code}: {body}") from None
    raise RuntimeError("unreachable")


def _multipart(fields, file_field, filename, content, ctype="video/mp4"):
    boundary = "kerbmap" + uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
               f'Content-Type: {ctype}\r\n\r\n'.encode())
    out.append(content)
    out.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def vss_upload(filename, content):
    s = _settings()
    fields = {"is_public": "true", "tags": s["tags"], "custom_prompt": INGEST_PROMPT, "camera_id": CAMERA_ID,
              "capture_type": s["capture_type"], "location": s["location"]}
    body, ctype = _multipart(fields, "file", filename, content)
    return _vss("/api/v1/videos/upload", data=body, headers={"Content-Type": ctype}, timeout=600)


def max_upload_mb():
    try:
        return float(((_vss("/api/v1/config", timeout=20) or {}).get("app") or {}).get("max_upload_size_mb"))
    except Exception:
        return None


def explore_items():
    items = {}
    for indexed in ("complete", "partial"):
        try:
            d = _vss("/api/v1/videos/explore", {"scope": "mine", "indexed": indexed, "limit": 100})
        except RuntimeError:
            if indexed == "partial":
                continue
            raise
        for c in d.get("chunks") or []:
            items.setdefault(c.get("original_video") or c.get("filename"), c)
    return list(items.values())


def match_parents(items, upload_name, object_key=None):
    """Explore items that are this upload: filename (or object key) contains the unique upload stem."""
    stem = upload_name.rsplit(".", 1)[0]
    key_base = (object_key or "").rsplit("/", 1)[-1]
    out = []
    for c in items:
        fn = c.get("filename") or ""
        ov = c.get("original_video") or ""
        if stem in fn or stem in ov or (key_base and (fn == key_base or ov.endswith("/" + key_base))):
            out.append(c)
    return sorted(out, key=lambda c: c.get("filename") or "")


def _indexed(c):
    total = int(c.get("total_segments") or 0)
    return total > 0 and len(c.get("timeline") or []) >= total


def fetch_detections(source, tries=3, wait=3.0):
    for i in range(tries):
        try:
            return _vss("/api/v1/videos/detections", {"source": source})
        except RuntimeError:
            if i == tries - 1:
                return None
            time.sleep(wait)


# ---------------------------------------------------------------- samples (known GPS)

_samples = None


def samples():
    global _samples
    if _samples is None:
        _samples = {}
        for path in (os.environ.get("KERB_SAMPLES_GPS"), os.path.join(HERE, "samples_gps.json")):
            if path and os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    _samples = json.load(fh).get("samples", {})
                break
    return _samples


def find_sample(sample_id, filename):
    """(sample id, sample) by id, else by the uploaded file name; (None, None) if neither matches."""
    s = samples()
    if sample_id and sample_id in s:
        return sample_id, s[sample_id]
    for sid, v in s.items():
        if filename and v.get("filename") and v["filename"] == filename:
            return sid, v
    return None, None


# ---------------------------------------------------------------- the job

def run_job(job, content, track, osm_cached=None):
    s = _settings()
    try:
        # 1 upload (a sample with no file: reuse the newest copy of that sample already in VAST)
        if content is None:
            _step(job, 0, "running", "looking for this sample in VAST")
            prior = [c for c in match_parents(explore_items(), job["upload_name"]) if _indexed(c)]
            if not prior:
                raise RuntimeError("this sample is not in VAST yet; upload the clip file")
            newest = max(prior, key=lambda c: c.get("upload_timestamp") or "")
            job["upload_name"] = newest.get("filename") or job["upload_name"]
            _set(job, object_key=(newest.get("original_video") or "").split("/", 3)[-1] or None, state="uploaded",
                 message="Sample already in VAST")
            _step(job, 0, "done", f"already in VAST: {newest.get('filename')} (uploaded {newest.get('upload_timestamp')})")
        else:
            _step(job, 0, "running", f"{len(content) / 1e6:.1f} MB")
            limit = max_upload_mb()
            if limit and len(content) > limit * 1e6:
                raise RuntimeError(f"clip is {len(content) / 1e6:.1f} MB, VAST accepts up to {limit:g} MB")
            res = vss_upload(job["upload_name"], content)
            if not res.get("success", True):
                raise RuntimeError("VAST refused the upload: " + str(res.get("message") or res)[:200])
            _set(job, object_key=res.get("object_key"), state="uploaded", message="Uploaded; waiting for VAST to index")
            _step(job, 0, "done", f"{len(content) / 1e6:.1f} MB, object {res.get('object_key')}")

        # 2 indexing
        _step(job, 1, "running", "waiting for the new video to appear")
        _set(job, state="indexing")
        t0 = time.time()
        while True:
            parents = match_parents(explore_items(), job["upload_name"], job["object_key"])
            done = sum(len(c.get("timeline") or []) for c in parents)
            total = sum(int(c.get("total_segments") or 0) for c in parents)
            _set(job, segments_found=done)
            if parents and all(_indexed(c) for c in parents):
                break
            waited = time.time() - t0
            detail = (f"{done} of {total} segments indexed" if parents else "upload received, segmenting")
            _step(job, 1, "running", f"{detail} ({waited:.0f} s)")
            _set(job, message=f"VAST is indexing: {detail}")
            if waited > s["timeout_s"]:
                raise RuntimeError(f"VAST indexing did not finish within {s['timeout_s']:.0f} s ({detail}). "
                                   f"The upload stays in VAST; try this job again later or use the prepared job.")
            time.sleep(s["poll_s"])
        segs, offset = [], 0.0
        for c in parents:
            for tl in sorted(c.get("timeline") or [], key=lambda x: x.get("segment_number") or 0):
                segs.append({"source": tl["source"], "segment_number": tl.get("segment_number") or len(segs) + 1,
                             "start_sec": offset + float(tl.get("segment_start_sec") or 0.0)})
            offset += float(c.get("chunk_duration_sec") or 30.0)
        _set(job, original_video=parents[0].get("original_video"), segments_found=len(segs))
        _step(job, 1, "done", f"{len(segs)} segments indexed in {time.time() - t0:.0f} s")

        # 3 kerb analysis
        _set(job, state="processing", message="Kerb analysis")
        _step(job, 2, "running", f"reading detections for {len(segs)} segments")
        missing = 0
        for seg in segs:
            seg["det"] = fetch_detections(seg["source"])
            missing += seg["det"] is None
        segs_ok = [g for g in segs if g["det"]]
        if not segs_ok:
            raise RuntimeError("VAST has no detections for any segment of this clip yet")
        if osm_cached:
            osm_el, osm_note = osm_cached, "OpenStreetMap, cached for this demo area"
        else:
            osm_el, osm_note = kerbpipe.fetch_osm(track)

        def fetch_video(src):
            with evidence.open_s3(src) as r:
                return r.read()
        result = kerbpipe.run(segs_ok, track, fetch_video=fetch_video, osm_elements=osm_el, osm_note=osm_note)
        st = result["stats"]
        _step(job, 2, "done", f"{st['keyframes']} keyframes, {st['slots']} kerb sightings; map: {st['map']}; "
                              f"model checks: {st['cosmos_calls']}"
                              + (f"; {missing} segment(s) without detections" if missing else ""))

        # 4 save
        _step(job, 3, "running", "writing receipts")
        spaces, obs, trail = kerbpipe.build_rows(result, {**job, "camera_id": CAMERA_ID}, _now())
        d = store.db()
        d.insert("kerb_trail", trail)
        d.insert("kerb_observations", obs)
        d.insert("kerb_spaces", spaces)
        by_state = {k: sum(1 for r in spaces if r["state"] == k) for k in kerbpipe.STATES}
        center = _center(spaces, track)
        _step(job, 3, "done", f"{len(spaces)} receipts, {len(obs)} observations, {len(trail)} trail steps "
                              f"({d.backend})")
        _set(job, state="done", receipts_by_state=by_state, center=center,
             message=f"{len(spaces)} kerb receipts ready" + ("" if center else " (position uncertain: no GPS)"))
    except Exception as exc:
        _fail(job, str(exc) or type(exc).__name__)


def _center(spaces, track):
    pts = [(r["lat"], r["lon"]) for r in spaces if r.get("lat") is not None and r.get("position_status") == "ok"]
    if pts:
        return {"lat": round(sum(p[0] for p in pts) / len(pts), 6), "lon": round(sum(p[1] for p in pts) / len(pts), 6)}
    if track:
        return {"lat": track[0]["lat"], "lon": track[0]["lon"]}
    return None


def job_from_receipts(job_id):
    """Rebuild a finished job from its saved receipts (after a restart, or a job run by another instance)."""
    if not re.fullmatch(r"[0-9a-f]{6,32}", job_id or ""):
        return None
    prefix = f"up_{job_id}_"
    rows = [r for r in store.read_all("kerb_spaces", {"camera_id": CAMERA_ID}, limit=100000)
            if r["space_id"].startswith(prefix)]
    if not rows:
        return None
    filename = object_key = None
    for t in store.trail_for(rows[0]["space_id"]):
        if t.get("step") == "gate":
            try:
                det = json.loads(t.get("detail_json") or "{}")
                filename, object_key = det.get("filename"), det.get("object_key")
            except ValueError:
                pass
    at = max((r.get("last_seen") or "") for r in rows) or None
    by_state = {k: sum(1 for r in rows if r.get("state") == k) for k in kerbpipe.STATES}
    return {"job_id": job_id, "state": "done", "filename": filename, "object_key": object_key,
            "steps": [{"name": n, "status": "done", "at": at, "detail": "from saved receipts"} for n in STEPS],
            "segments_found": len({r.get("segment_number") for r in rows}), "receipts_by_state": by_state,
            "center": _center(rows, None), "message": f"{len(rows)} kerb receipts ready"}


PUBLIC = ("job_id", "state", "steps", "filename", "object_key", "segments_found", "receipts_by_state", "center",
          "message")


def public_job(job):
    out = {k: job.get(k) for k in PUBLIC}
    out["gps"] = job.get("gps")
    return out


def get_job(job_id):
    with _lock:
        job = _jobs.get(job_id)
    if job is None:
        job = _load_file().get(job_id)
        if job and job.get("state") not in ("done", "error"):
            job = {**job, "state": "error", "message": "the app restarted while this job was running; upload again"}
    if job is None:
        job = job_from_receipts(job_id)
    return job


# ---------------------------------------------------------------- routes

def _parse_form(body, ctype):
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body)
    if not msg.is_multipart():
        raise HTTPException(400, "send multipart/form-data with a 'file' field")
    form = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            form[name] = (part.get_filename(), part.get_payload(decode=True) or b"")
    return form


@router.post("/api/upload")
async def upload(request: Request):
    ctype = request.headers.get("content-type") or ""
    if not ctype.startswith("multipart/form-data"):
        raise HTTPException(400, "send multipart/form-data with a 'file' field")
    form = _parse_form(await request.body(), ctype)
    fname, content = form.get("file", (None, b""))
    sample_id = (form.get("sample_id", (None, b""))[1] or b"").decode("utf-8", "replace").strip() or None
    gps_text = (form.get("gps", (None, b""))[1] or b"").decode("utf-8", "replace").strip()
    if sample_id and sample_id not in samples():
        raise HTTPException(400, f"unknown sample_id {sample_id!r}")
    if not fname or not content:
        if not sample_id or not samples()[sample_id].get("filename"):
            raise HTTPException(400, "missing 'file' (a video)")
        fname, content = samples()[sample_id]["filename"], None   # reuse the sample already in VAST
    fname = os.path.basename(fname.replace("\\", "/"))
    track, track_source = None, None
    if gps_text:
        track = kerbpipe.parse_gps_csv(gps_text)
        if not track:
            raise HTTPException(400, "gps CSV has no rows with t_sec,lat,lon")
        track_source = f"uploaded CSV ({len(track)} points)"
    sid, sample = find_sample(sample_id, fname)
    if not gps_text and sample:
        track = kerbpipe.track_from_rows(sample["gps"])
        track_source = f"sample {sid}" + ("" if sid == sample_id else " (matched by file name)")
    job = new_job(fname, track_source or "none (positions uncertain)")
    stem, ext = os.path.splitext(fname)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem)[:60] or "clip"
    job["upload_name"] = f"{stem}_{job['job_id']}{ext.lower() or '.mp4'}" if content is not None else fname
    job["sample_id"] = sid
    _save()
    osm = (sample or {}).get("osm")
    threading.Thread(target=run_job, args=(job, content, track, osm), daemon=True, name=f"upload-{job['job_id']}").start()
    return {"job_id": job["job_id"]}


@router.get("/api/upload/latest-done")
def latest_done():
    """The prepared fallback: KERB_DEMO_JOB if set, else the newest finished job (memory, jobs file, receipts)."""
    fixed = os.environ.get("KERB_DEMO_JOB")
    if fixed and get_job(fixed):
        return {"job_id": fixed}
    with _lock:
        jobs = list(_jobs.values())
    jobs += [j for k, j in _load_file().items() if k not in _jobs]
    done = [j for j in jobs if j.get("state") == "done"]
    if done:
        return {"job_id": max(done, key=lambda j: j.get("created") or "")["job_id"]}
    rows = [r for r in store.read_all("kerb_spaces", {"camera_id": CAMERA_ID}, limit=100000) if store.job_of(r)]
    if rows:
        return {"job_id": store.job_of(max(rows, key=lambda r: r.get("last_seen") or ""))}
    raise HTTPException(404, "no finished upload job yet")


@router.get("/api/samples")
@router.get("/api/upload/samples")
def upload_samples():
    return {"samples": [{"sample_id": k, "filename": v.get("filename"), "label": v.get("label"),
                         "gps_points": len(v.get("gps") or [])} for k, v in samples().items()]}


@router.get("/api/upload/{job_id}")
def upload_status(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "no such upload job")
    return public_job(job)
