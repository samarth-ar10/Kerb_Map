"""Freeze a human label sample, then score the existing receipts once.

Sets were named before any score: set01 and set02 are the two drives the slot
step was built on (dev, 30 spots each). set03 is the next drive (held-out, 40).
The sample uses only identity fields. Predictions are read after the label
file's sha256 is stored.

Metric definitions are fixed here. Nothing in score() searches thresholds,
and a second run refuses once dashcam/RESULTS.md exists.
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPACES = ROOT / "dashcam" / "out" / "kerb_spaces.jsonl"
CACHE = ROOT / "dashcam" / "out" / "cosmos_cache.jsonl"
SHARE = ROOT / "dashcam" / "out" / "cosmos_share.json"
SAMPLE = ROOT / "dashcam" / "out" / "eval_sample.json"
PROTOCOL = ROOT / "dashcam" / "out" / "eval_protocol.json"
FRAMES = ROOT / "dashcam" / "out" / "eval_frames"
LABELS = ROOT / "data" / "labels" / "2026-10-17-kerb-spots.csv"
CASES = ROOT / "dashcam" / "out" / "kerb_eval_cases.jsonl"
RESULTS = ROOT / "dashcam" / "RESULTS.md"
MAP_PATH = ROOT / "data" / "pie_map.csv"

DEV_SETS = ("set01", "set02")
HELD_OUT = "set03"
PER_DEV = 30
N_HELD = 40
SEED = 20261017
ID_FIELDS = ("space_id", "set", "chunk", "segment_number", "time_sec", "side", "video")

# A receipt counts as a real-space answer only in these states.
REAL_STATES = frozenset({"free", "occupied"})
ANSWERED_OCCUPANCY = frozenset({"free", "occupied"})


def _load_ids() -> list[dict]:
    rows = []
    with SPACES.open() as handle:
        for line in handle:
            raw = json.loads(line)
            rows.append({key: raw[key] for key in ID_FIELDS})
    return rows


def sample_spots() -> list[dict]:
    if SAMPLE.is_file():
        raise SystemExit(f"{SAMPLE} already exists; the draw is not redone")
    rng = random.Random(SEED)
    pool = _load_ids()
    chosen: list[dict] = []
    for set_id in DEV_SETS:
        group = [row for row in pool if row["set"] == set_id]
        if len(group) < PER_DEV:
            raise SystemExit(f"{set_id} has {len(group)} spots, need {PER_DEV}")
        picked = rng.sample(group, PER_DEV)
        for row in picked:
            row["split"] = "dev"
        chosen.extend(picked)
    held = [row for row in pool if row["set"] == HELD_OUT]
    if len(held) < N_HELD:
        raise SystemExit(f"{HELD_OUT} has {len(held)} spots, need {N_HELD}")
    for row in rng.sample(held, N_HELD):
        row["split"] = "held_out"
        chosen.append(row)
    for index, row in enumerate(chosen, start=1):
        row["case_id"] = f"eval-{index:03d}"
        row["frame"] = f"dashcam/out/eval_frames/{row['case_id']}.jpg"
    PROTOCOL.parent.mkdir(parents=True, exist_ok=True)
    PROTOCOL.write_text(
        json.dumps(
            {
                "dev_sets": list(DEV_SETS),
                "held_out_set": HELD_OUT,
                "per_dev_set": PER_DEV,
                "held_out": N_HELD,
                "seed": SEED,
                "set_choice": (
                    "set01 and set02 are the drives the slot step was built on; "
                    "set03 is the next drive. Not chosen from a score."
                ),
            },
            indent=2,
        )
    )
    SAMPLE.write_text(json.dumps(chosen, indent=2))
    return chosen


def _bbox_for(record: dict, payload: dict) -> list[int] | None:
    from boxes import frame_at
    from slots import slots_for_frame

    frame = frame_at(payload["frames"], float(record["time_sec"]))
    for index, slot in enumerate(slots_for_frame(frame, payload["width"], payload["height"])):
        space_id = (
            f"{record['set']}_c{int(record['chunk']):04d}"
            f"_s{int(record['segment_number']):02d}_t{float(record['time_sec']):.1f}"
            f"_{slot['side']}_{index}"
        )
        if space_id == record["space_id"]:
            box = slot.get("bbox")
            return [int(value) for value in box] if box else None
    return None


def _grab(backend: str, token: str, record: dict, bbox: list[int] | None, width: int, height: int) -> None:
    from session import stream_url

    dest = ROOT / record["frame"]
    if dest.is_file() and dest.stat().st_size > 0:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = stream_url(backend, token, record["video"])
    filters = []
    if bbox and width > 0 and height > 0:
        x1, y1, x2, y2 = bbox
        x1 = min(max(x1, 0), width - 2)
        y1 = min(max(y1, 0), height - 2)
        x2 = min(max(x2, x1 + 2), width - 1)
        y2 = min(max(y2, y1 + 2), height - 1)
        filters.append(
            f"drawbox=x={x1}:y={y1}:w={x2 - x1}:h={y2 - y1}:color=yellow:t=6"
        )
    filters.append("scale=360:-2")
    command = [
        "ffmpeg", "-y", "-ss", f"{float(record['time_sec']):.3f}", "-i", url,
        "-frames:v", "1", "-vf", ",".join(filters), str(dest),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=90)
    if completed.returncode != 0 or not dest.is_file():
        raise RuntimeError((completed.stderr or "ffmpeg failed")[-180:])


def prepare_frames(rows: list[dict]) -> dict:
    from boxes import fetch_segment
    from session import login

    backend, token = login()
    by_video: dict[str, list[dict]] = {}
    for row in rows:
        by_video.setdefault(row["video"], []).append(row)
    payloads: dict[str, dict] = {}
    errors: list[str] = []

    def load(video: str) -> tuple[str, dict | str]:
        try:
            return video, fetch_segment(backend, token, video)
        except Exception as exc:  # noqa: BLE001 — one bad segment should not drop the sample
            return video, str(exc)[:180]

    with ThreadPoolExecutor(max_workers=2) as pool:
        for video, result in pool.map(load, list(by_video)):
            if isinstance(result, str):
                errors.append(f"{video}: {result}")
            else:
                payloads[video] = result

    jobs = []
    for row in rows:
        payload = payloads.get(row["video"])
        if payload is None:
            errors.append(f"{row['case_id']}: no detections")
            continue
        try:
            bbox = _bbox_for(row, payload)
        except Exception as exc:  # noqa: BLE001
            bbox = None
            errors.append(f"{row['case_id']}: box {exc}"[:180])
        jobs.append((row, bbox, int(payload["width"]), int(payload["height"])))

    def shoot(job: tuple[dict, list[int] | None, int, int]) -> str | None:
        row, bbox, width, height = job
        try:
            _grab(backend, token, row, bbox, width, height)
        except Exception as exc:  # noqa: BLE001
            return f"{row['case_id']}: {exc}"[:180]
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        for failure in pool.map(shoot, jobs):
            if failure:
                errors.append(failure)
    written = sum(1 for row in rows if (ROOT / row["frame"]).is_file())
    return {"frames": written, "errors": errors}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_label_csv(rows: list[dict]) -> None:
    LABELS.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "case_id", "split", "set", "chunk", "segment_number", "time_sec", "side",
        "space_id", "frame", "legal", "occupancy", "labeler",
    ]
    with LABELS.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in fields})


def _cases_from_csv() -> list[dict]:
    digest = _sha256(LABELS)
    frozen_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cases = []
    with LABELS.open(newline="") as handle:
        for row in csv.DictReader(handle):
            row["frozen_sha256"] = digest
            row["frozen_at"] = frozen_at
            cases.append(row)
    return cases


def _write_vastdb(cases: list[dict]) -> str:
    try:
        import pyarrow as pa
        import vastdb
    except ImportError:
        return "vastdb not installed; wrote JSONL only"
    env: dict[str, str] = {}
    for raw in sorted(Path("/config").glob("*.config"))[0].read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    endpoint = env["S3_ENDPOINT"]
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    session = vastdb.connect(
        endpoint=endpoint, access=env["ACCESS_KEY"], secret=env["SECRET_KEY"], ssl_verify=False,
    )
    schema = pa.schema([(key, pa.utf8()) for key in cases[0].keys()])
    columns = {
        field.name: ["" if row.get(field.name) is None else str(row.get(field.name)) for row in cases]
        for field in schema
    }
    table_data = pa.table(columns, schema=schema)
    with session.transaction() as tx:
        bucket = tx.bucket(env["VASTDB_BUCKET"]).schema(env.get("VDB_SCHEMA", "vss-schema"))
        table = bucket.table("kerb_eval_cases", fail_if_missing=False)
        if table is None:
            table = bucket.create_table("kerb_eval_cases", columns=schema, fail_if_exists=False)
        else:
            existing = table.select(columns=["case_id"]).read_all()
            if existing.num_rows:
                return f"kerb_eval_cases already has {existing.num_rows} rows; left them in place"
        table.insert(table_data)
    return f"inserted {len(cases)} rows into {env.get('VDB_SCHEMA', 'vss-schema')}.kerb_eval_cases"


def freeze(rows: list[dict]) -> dict:
    sample = json.loads(SAMPLE.read_text())
    expected = {row["case_id"] for row in sample}
    got = {row["case_id"] for row in rows}
    if expected != got:
        raise SystemExit("label rows do not match the frozen sample")
    for row in rows:
        if row.get("legal") not in {"yes", "no"} or row.get("occupancy") not in {"free", "occupied"}:
            raise SystemExit(f"{row['case_id']} is missing a legal or occupancy label")
        name = (row.get("labeler") or "").strip()
        if len(name) < 2 or name.lower() in {"ai", "agent", "cursor", "model", "cosmos"}:
            raise SystemExit("labeler must be the person who looked at the frames")
    by_id = {row["case_id"]: row for row in sample}
    ordered = []
    for row in rows:
        base = by_id[row["case_id"]]
        ordered.append({**base, "legal": row["legal"], "occupancy": row["occupancy"], "labeler": row["labeler"].strip()})
    ordered.sort(key=lambda item: item["case_id"])
    write_label_csv(ordered)
    cases = _cases_from_csv()
    CASES.parent.mkdir(parents=True, exist_ok=True)
    with CASES.open("w") as handle:
        for row in cases:
            handle.write(json.dumps(row) + "\n")
    try:
        db_status = _write_vastdb(cases)
    except Exception as exc:  # noqa: BLE001 — JSONL is the copy that scoring reads
        db_status = f"vastdb insert failed: {exc}"[:400]
    digest = cases[0]["frozen_sha256"]
    return {"frozen_sha256": digest, "frozen_at": cases[0]["frozen_at"], "vast": db_status, "n": len(cases)}


def _predictions() -> dict[str, dict]:
    found = {}
    with SPACES.open() as handle:
        for line in handle:
            row = json.loads(line)
            found[row["space_id"]] = row
    return found


def _sent_ids() -> set[str]:
    if not CACHE.is_file():
        return set()
    sent = set()
    with CACHE.open() as handle:
        for line in handle:
            if line.strip():
                sent.add(json.loads(line)["space_id"])
    return sent


def _reference_gps() -> dict[tuple[str, str], tuple[float, float, float, float]]:
    found = {}
    with MAP_PATH.open(newline="") as handle:
        for row in csv.DictReader(handle):
            found[(row["set"], str(int(row["chunk"])))] = (
                float(row["lat_start"]), float(row["lon_start"]),
                float(row["lat_end"]), float(row["lon_end"]),
            )
    return found


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _rate(hit: int, total: int) -> str:
    if total == 0:
        return f"{hit}/{total} (undefined)"
    return f"{hit}/{total} = {hit / total:.3f}"


def _split_metrics(rows: list[dict], predictions: dict[str, dict], sent: set[str], gps: dict) -> dict:
    n = len(rows)
    real_hit = real_n = 0
    occ_hit = occ_n = 0
    free_hit = free_n = 0
    legal_n = legal_found = 0
    free_truth = free_found = 0
    cannot = 0
    pin_errors: list[float] = []
    uncertain = 0
    sent_n = 0
    failures = []
    for row in rows:
        pred = predictions.get(row["space_id"])
        if pred is None:
            continue
        state = pred["state"]
        legal = row["legal"] == "yes"
        human_free = row["occupancy"] == "free"
        if state in REAL_STATES:
            real_n += 1
            if legal:
                real_hit += 1
        if legal and state in ANSWERED_OCCUPANCY:
            occ_n += 1
            if state == row["occupancy"]:
                occ_hit += 1
        if state == "free":
            free_n += 1
            if legal and human_free:
                free_hit += 1
        if legal:
            legal_n += 1
            if state in REAL_STATES:
                legal_found += 1
        if legal and human_free:
            free_truth += 1
            if state == "free":
                free_found += 1
        if state == "cannot_tell":
            cannot += 1
        if row["space_id"] in sent:
            sent_n += 1
        status = pred.get("position_status") or ""
        if status != "uncertain" and pred.get("lat") not in (None, ""):
            ref = gps.get((row["set"], str(int(row["chunk"]))))
            if ref is not None:
                pin_errors.append(_haversine_m(float(pred["lat"]), float(pred["lon"]), ref[0], ref[1]))
        else:
            uncertain += 1
        wrong_real = state in REAL_STATES and not legal
        wrong_occ = legal and state in ANSWERED_OCCUPANCY and state != row["occupancy"]
        missed = legal and state not in REAL_STATES and state != "cannot_tell"
        if wrong_real or wrong_occ or missed:
            failures.append(
                {
                    "case_id": row["case_id"],
                    "split": row["split"],
                    "set": row["set"],
                    "side": row["side"],
                    "frame": row["frame"],
                    "human_legal": row["legal"],
                    "human_occupancy": row["occupancy"],
                    "state": state,
                    "reason": pred.get("reason") or "",
                }
            )
    failures.sort(key=lambda item: item["case_id"])
    return {
        "n": n,
        "real_space_precision": _rate(real_hit, real_n),
        "free_vs_occupied_accuracy": _rate(occ_hit, occ_n),
        "free_precision": _rate(free_hit, free_n),
        "space_recall": _rate(legal_found, legal_n),
        "free_recall": _rate(free_found, free_truth),
        "cannot_tell_rate": _rate(cannot, n),
        "pin_n": len(pin_errors),
        "pin_median_m": None if not pin_errors else round(_median(pin_errors), 2),
        "position_uncertain": uncertain,
        "sent": sent_n,
        "failures": failures,
    }


def score() -> dict:
    if RESULTS.is_file():
        raise SystemExit(f"{RESULTS} already exists; not scoring again")
    if not LABELS.is_file() or not CASES.is_file():
        raise SystemExit("labels are not frozen")
    digest = _sha256(LABELS)
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    if not cases or cases[0]["frozen_sha256"] != digest:
        raise SystemExit("label file hash does not match kerb_eval_cases")
    predictions = _predictions()
    sent = _sent_ids()
    gps = _reference_gps()
    dev = _split_metrics([row for row in cases if row["split"] == "dev"], predictions, sent, gps)
    held = _split_metrics([row for row in cases if row["split"] == "held_out"], predictions, sent, gps)
    share = json.loads(SHARE.read_text()) if SHARE.is_file() else {}
    examples = (dev["failures"] + held["failures"])[:3]
    text = _render(cases[0], dev, held, share, examples)
    RESULTS.write_text(text)
    return {"results": str(RESULTS), "frozen_sha256": digest, "examples": len(examples)}


def _render(meta: dict, dev: dict, held: dict, share: dict, examples: list[dict]) -> str:
    def block(title: str, metrics: dict) -> str:
        pin = "no pin on these receipts" if metrics["pin_n"] == 0 else f"{metrics['pin_median_m']} m"
        return "\n".join(
            [
                f"## {title}",
                "",
                "| Measure | Result |",
                "|---|---|",
                f"| Real-space precision | {metrics['real_space_precision']} |",
                f"| Free vs occupied accuracy | {metrics['free_vs_occupied_accuracy']} |",
                f"| Free precision | {metrics['free_precision']} |",
                f"| Space recall | {metrics['space_recall']} |",
                f"| Free recall | {metrics['free_recall']} |",
                f"| Cannot-tell rate | {metrics['cannot_tell_rate']} |",
                f"| Pin error, median | {pin} over {metrics['pin_n']} pins; position uncertain on {metrics['position_uncertain']} |",
                f"| Sample spots sent to Cosmos | {metrics['sent']}/{metrics['n']} |",
                "",
            ]
        )

    lines = [
        "# Dashcam results",
        "",
        "Scored once from the frozen label file. Dev is set01 and set02 (30 spots each). Held-out is set03 (40 spots). The held-out numbers were not used to change a threshold.",
        "",
        f"Label file: `data/labels/2026-10-17-kerb-spots.csv`.",
        f"frozen_sha256: `{meta['frozen_sha256']}`.",
        f"Frozen at {meta['frozen_at']} by {meta['labeler']}.",
        "",
        "A real-space answer is a receipt in state free or occupied. Precision uses those answers. Free-vs-occupied accuracy uses only the ones a person marked legal. Space recall is legal spots the receipt called free or occupied. Free recall is legal free spots the receipt called free. Cannot-tell is that state divided by the sample.",
        "",
        block(f"Dev (n={dev['n']})", dev),
        block(f"Held-out (n={held['n']})", held),
        "## Cosmos share on the full run",
        "",
        f"Slots sent: {share.get('sent')}/{share.get('slots')} = {share.get('share')}. This is the pipeline share, not a subset chosen after scoring.",
        "",
        "## Three failures",
        "",
    ]
    if not examples:
        lines.append("No wrong real-space, occupancy, or missed-legal case in case-id order.")
    for item in examples:
        lines.extend(
            [
                f"### {item['case_id']} ({item['split']}, {item['set']}, {item['side']})",
                "",
                f"Person: legal {item['human_legal']}, {item['human_occupancy']}. Receipt state: {item['state']}. Reason: {item['reason'] or 'none'}.",
                "",
                f"![{item['case_id']}]({item['frame'].removeprefix('dashcam/')})",
                "",
            ]
        )
    lines.append("")
    return "\n".join(lines)


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Kerb labels</title>
<style>
  body { font-family: sans-serif; margin: 24px; max-width: 920px; }
  img { width: 100%; background: #111; }
  button { font-size: 18px; margin: 6px 6px 6px 0; padding: 8px 14px; }
  button.on { outline: 3px solid #084; }
  #side { font-size: 28px; margin: 8px 0; }
</style>
</head>
<body>
<p>Label only what is inside the yellow box, on the named kerb. Legal parking space or not, then free or occupied. Predictions are not on this page.</p>
<label>Your name <input id="who" autocomplete="name"></label>
<p id="progress"></p>
<p id="side"></p>
<img id="frame" alt="kerb frame">
<p>
  <button data-k="legal" data-v="yes">Legal space</button>
  <button data-k="legal" data-v="no">Not a legal space</button>
</p>
<p>
  <button data-k="occupancy" data-v="free">Free</button>
  <button data-k="occupancy" data-v="occupied">Occupied</button>
</p>
<p><button id="next">Save and next</button></p>
<p id="msg"></p>
<script>
const cases = __CASES__;
let i = 0;
const answers = {};
function show() {
  const row = cases[i];
  document.getElementById("progress").textContent = (i + 1) + " / " + cases.length + "  " + row.split + "  " + row.case_id;
  document.getElementById("side").textContent = row.side.toUpperCase() + " kerb  ·  " + row.set + " chunk " + row.chunk + "  t=" + row.time_sec + "s";
  document.getElementById("frame").src = "/" + row.frame;
  document.querySelectorAll("button[data-k]").forEach(btn => btn.classList.remove("on"));
}
document.querySelectorAll("button[data-k]").forEach(btn => {
  btn.onclick = () => {
    const row = cases[i];
    answers[row.case_id] = answers[row.case_id] || {};
    answers[row.case_id][btn.dataset.k] = btn.dataset.v;
    document.querySelectorAll('button[data-k="' + btn.dataset.k + '"]').forEach(other => other.classList.remove("on"));
    btn.classList.add("on");
  };
});
document.getElementById("next").onclick = async () => {
  const name = document.getElementById("who").value.trim();
  const row = cases[i];
  const got = answers[row.case_id] || {};
  const msg = document.getElementById("msg");
  if (name.length < 2) { msg.textContent = "Enter your name first."; return; }
  if (!got.legal || !got.occupancy) { msg.textContent = "Choose legal or not, and free or occupied."; return; }
  got.labeler = name;
  got.case_id = row.case_id;
  msg.textContent = "";
  if (i < cases.length - 1) { i += 1; show(); return; }
  const payload = cases.map(item => ({...answers[item.case_id], case_id: item.case_id}));
  if (payload.some(item => !item.legal)) { msg.textContent = "A earlier spot was skipped."; return; }
  msg.textContent = "Freezing the file…";
  const response = await fetch("/freeze", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
  const body = await response.json();
  msg.textContent = response.ok ? ("Frozen " + body.frozen_sha256) : body.error;
};
show();
</script>
</body>
</html>
"""


def serve(port: int) -> None:
    rows = json.loads(SAMPLE.read_text())
    public = [
        {key: row[key] for key in ("case_id", "split", "set", "chunk", "time_sec", "side", "frame")}
        for row in rows
    ]
    page = PAGE.replace("__CASES__", json.dumps(public))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path in {"/", "/index.html"}:
                self._send(200, page.encode(), "text/html; charset=utf-8")
                return
            rel = self.path.lstrip("/")
            path = (ROOT / rel).resolve()
            if path.is_file() and FRAMES in path.parents:
                self._send(200, path.read_bytes(), "image/jpeg")
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/freeze":
                self._send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                posted = json.loads(self.rfile.read(length))
                meta = freeze(posted)
                scored = score()
                meta["results"] = scored["results"]
                self._send(200, json.dumps(meta).encode(), "application/json")
            except SystemExit as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
            except Exception as exc:  # noqa: BLE001 — show the freeze error in the page
                self._send(500, json.dumps({"error": str(exc)[:300]}).encode(), "application/json")

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "prepare"
    if command == "prepare":
        rows = json.loads(SAMPLE.read_text()) if SAMPLE.is_file() else sample_spots()
        frames = prepare_frames(rows)
        print(json.dumps({"cases": len(rows), **frames}), file=sys.stderr)
    elif command == "serve":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
        serve(port)
    elif command == "score":
        print(json.dumps(score()), file=sys.stderr)
    else:
        raise SystemExit("use prepare, serve, or score")


if __name__ == "__main__":
    main()
