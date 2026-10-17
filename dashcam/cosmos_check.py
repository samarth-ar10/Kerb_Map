"""Ask Cosmos about kerb gaps the rules left open, then write receipts.

Probe result this model actually returns (nvidia/cosmos3-nano-reasoner):
image_url works; reasoning and reasoning_content are null; the word is in
message.content; logprobs.content gives per-token log probabilities.
A keyframe is an image, so the call sends a JPEG, not a video.
Confidence is exp(sum of logprobs covering the answer word). Below 0.6 the
label is CANNOT_TELL. At most one request in flight per worker, two workers.
"""

from __future__ import annotations

import base64
import json
import math
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rules import load_positions
from session import login, stream_url
from slots import CLIPS, ROOT, collect_slots, trail_steps

GPU_HOST = "166.19.38.112"
COSMOS_URL = f"http://{GPU_HOST}:8001"
MODEL = "nvidia/cosmos3-nano-reasoner"
WORDS = ("NOT_A_SPACE", "CANNOT_TELL", "OCCUPIED", "FREE")
MIN_CONFIDENCE = 0.6
WORKERS = 2
CACHE = ROOT / "dashcam" / "out" / "cosmos_cache.jsonl"
PROBE = ROOT / "dashcam" / "out" / "cosmos_probe.json"
SPACES = ROOT / "dashcam" / "out" / "kerb_spaces.jsonl"
TRAIL = ROOT / "dashcam" / "out" / "kerb_trail.jsonl"
RECEIPTS = ROOT / "dashcam" / "out" / "receipts"
SETS = [f"set0{i}" for i in range(1, 7)]
PROMPT = (
    "The yellow rectangle marks one gap at the kerb. "
    "Reply with exactly one word: FREE, OCCUPIED, NOT_A_SPACE, or CANNOT_TELL."
)


def _gpu_token() -> str:
    configs = sorted(Path("/config").glob("*.config"))
    if len(configs) != 1:
        raise SystemExit(f"expected exactly one /config/*.config, found {len(configs)}")
    for raw in configs[0].read_text().splitlines():
        line = raw.strip()
        if line.startswith("GPU_BEARER_TOKEN="):
            token = line.split("=", 1)[1].strip()
            if token:
                return token
    raise SystemExit("GPU_BEARER_TOKEN is empty in the team config")


def _post(token: str, payload: dict) -> dict:
    request = urllib.request.Request(
        COSMOS_URL + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)


def _confidence(choice: dict, word: str | None) -> float | None:
    tokens = ((choice.get("logprobs") or {}).get("content")) or []
    if not tokens or not word:
        return None
    built = ""
    total = 0.0
    target = word.replace("_", "")
    for token in tokens:
        built += token.get("token") or ""
        total += float(token.get("logprob") or 0.0)
        flat = built.upper().replace(" ", "").replace("_", "")
        if target in flat:
            return math.exp(total)
    return math.exp(total)


def _label_from(choice: dict) -> tuple[str, float | None, str]:
    message = choice.get("message") or {}
    raw = (message.get("content") or "").strip().upper()
    word = next((item for item in WORDS if item in raw.replace(" ", "")), None)
    confidence = _confidence(choice, word)
    if word is None or confidence is None or confidence < MIN_CONFIDENCE:
        return "CANNOT_TELL", confidence, raw
    return word, confidence, raw


def _message(image_b64: str) -> dict:
    return {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": 16,
        "logprobs": True,
        "top_logprobs": 5,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            }
        ],
    }


def probe(token: str) -> dict:
    frames = sorted((ROOT / "dashcam" / "out" / "frames" / "set01").glob("*.jpg"))[:3]
    if len(frames) < 3:
        raise SystemExit(f"need 3 probe frames, found {len(frames)}")
    reports = []
    for path in frames:
        drawn = Path("/tmp") / f"cosmos_probe_{path.name}"
        subprocess.check_call(
            [
                "ffmpeg", "-y", "-i", str(path),
                "-vf", "drawbox=x=30:y=50:w=50:h=40:color=yellow:t=3",
                str(drawn),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        image = base64.b64encode(drawn.read_bytes()).decode()
        body = _post(token, _message(image))
        choice = body["choices"][0]
        message = choice["message"]
        label, confidence, raw = _label_from(choice)
        reports.append(
            {
                "frame": path.name,
                "content": message.get("content"),
                "reasoning_content": message.get("reasoning_content"),
                "reasoning": message.get("reasoning"),
                "logprob_tokens": len(((choice.get("logprobs") or {}).get("content")) or []),
                "label": label,
                "confidence": confidence,
                "raw": raw,
            }
        )
    summary = {
        "model": MODEL,
        "input": "image_url jpeg",
        "thinking_text": False,
        "logprobs": True,
        "frames": reports,
    }
    PROBE.parent.mkdir(parents=True, exist_ok=True)
    PROBE.write_text(json.dumps(summary, indent=2))
    return summary


def _grab_frame(backend: str, vss_token: str, record: dict, dest: Path) -> None:
    bbox = record.get("bbox") or [0, 0, 10, 10]
    x1, y1, x2, y2 = [int(value) for value in bbox]
    width = max(int(record["width"]), 1)
    height = max(int(record["height"]), 1)
    x1 = min(max(x1, 0), width - 2)
    y1 = min(max(y1, 0), height - 2)
    x2 = min(max(x2, x1 + 2), width - 1)
    y2 = min(max(y2, y1 + 2), height - 1)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = stream_url(backend, vss_token, record["video"])
    draw = f"drawbox=x={x1}:y={y1}:w={x2 - x1}:h={y2 - y1}:color=yellow:t=4,scale=640:-2"
    command = [
        "ffmpeg", "-y", "-ss", f"{float(record['time_sec']):.3f}", "-i", url,
        "-frames:v", "1", "-vf", draw, str(dest),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=90)
    if completed.returncode != 0 or not dest.is_file():
        raise RuntimeError((completed.stderr or "ffmpeg failed")[-200:])


def _load_cache() -> dict[str, dict]:
    found: dict[str, dict] = {}
    if not CACHE.is_file():
        return found
    for line in CACHE.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        found[row["space_id"]] = row
    return found


def _ask_one(record: dict, token: str, backend: str, vss_token: str, cache: dict, lock: threading.Lock) -> dict:
    cached = cache.get(record["space_id"])
    if cached:
        return cached
    dest = RECEIPTS / f"{record['space_id']}.jpg"
    _grab_frame(backend, vss_token, record, dest)
    image = base64.b64encode(dest.read_bytes()).decode()
    try:
        body = _post(token, _message(image))
        label, confidence, raw = _label_from(body["choices"][0])
        error = ""
    except Exception as exc:  # noqa: BLE001 — keep the receipt and mark it unsettled
        label, confidence, raw, error = "CANNOT_TELL", None, "", str(exc)[:240]
    answer = {
        "space_id": record["space_id"],
        "label": label,
        "confidence": confidence,
        "raw": raw,
        "error": error,
        "evidence_uri": str(dest.relative_to(ROOT)),
    }
    with lock:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        with CACHE.open("a") as handle:
            handle.write(json.dumps(answer) + "\n")
        cache[record["space_id"]] = answer
    return answer


def _state_for(label: str) -> str:
    return {
        "FREE": "free",
        "OCCUPIED": "occupied",
        "NOT_A_SPACE": "not_a_space",
        "CANNOT_TELL": "cannot_tell",
    }[label]


def _position(record: dict) -> tuple[str, str, str]:
    if record.get("lat") is None or record.get("lon") is None:
        return "uncertain", "position uncertain", "no GPS in data/pie_map.csv; not snapped to a road"
    return (
        "uncertain",
        "position uncertain",
        "GPS present but side offset was not snapped because no road geometry was loaded",
    )


def _receipt_text(record: dict, cosmos: dict | None, position_status: str) -> str:
    if cosmos is None:
        why = record["reason"] or record["state"]
        return (
            f"{record['side']} kerb at {record['set']} chunk {record['chunk']} "
            f"t={record['time_sec']}s is {record['state']} ({why}). "
            f"Cosmos was not asked. Position {position_status}."
        )
    conf = cosmos.get("confidence")
    conf_text = "none" if conf is None else f"{conf:.3f}"
    return (
        f"{record['side']} kerb gap at {record['set']} chunk {record['chunk']} "
        f"t={record['time_sec']}s. Rules left it open. "
        f"Cosmos said {cosmos['label']} with confidence {conf_text}. "
        f"Position {position_status}."
    )


def _write_vastdb(spaces: list[dict], trail: list[dict]) -> str:
    try:
        import pyarrow as pa
        import vastdb
    except ImportError:
        return "vastdb not installed; wrote JSONL only"
    configs = sorted(Path("/config").glob("*.config"))
    env: dict[str, str] = {}
    for raw in configs[0].read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    endpoint = env["S3_ENDPOINT"]
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    session = vastdb.connect(
        endpoint=endpoint,
        access=env["ACCESS_KEY"],
        secret=env["SECRET_KEY"],
        ssl_verify=False,
    )
    space_schema = pa.schema([(key, pa.utf8()) for key in spaces[0].keys()])
    trail_schema = pa.schema(
        [(key, pa.float32() if key == "confidence" else pa.utf8()) for key in trail[0].keys()]
    )

    def rows(items: list[dict], schema: pa.Schema) -> pa.Table:
        columns = {}
        for field in schema:
            values = []
            for item in items:
                value = item.get(field.name)
                if field.type == pa.float32():
                    values.append(None if value is None or value == "" else float(value))
                else:
                    values.append("" if value is None else str(value))
            columns[field.name] = values
        return pa.table(columns, schema=schema)

    with session.transaction() as tx:
        schema = tx.bucket(env["VASTDB_BUCKET"]).schema(env.get("VDB_SCHEMA", "vss-schema"))
        for name, table_schema, data in (
            ("kerb_spaces", space_schema, spaces),
            ("kerb_trail", trail_schema, trail),
        ):
            table = schema.table(name, fail_if_missing=False)
            if table is None:
                table = schema.create_table(name, columns=table_schema, fail_if_exists=False)
            table.insert(rows(data, table_schema))
    return f"inserted into {env.get('VDB_SCHEMA', 'vss-schema')}.kerb_spaces and kerb_trail"


def main() -> None:
    token = _gpu_token()
    probe_summary = probe(token)
    print(
        json.dumps({"probe_frames": len(probe_summary["frames"]), "input": probe_summary["input"]}),
        file=sys.stderr,
    )
    records = collect_slots(SETS, CLIPS)
    gaps = [record for record in records if record["state"] == "gap"]
    limit = max((len(records) * 5 - 1) // 100, 0)
    to_send = gaps[:limit]
    backend, vss_token = login()
    cache = _load_cache()
    lock = threading.Lock()
    answers: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [
            pool.submit(_ask_one, record, token, backend, vss_token, cache, lock) for record in to_send
        ]
        for future in as_completed(futures):
            answer = future.result()
            answers[answer["space_id"]] = answer
    share = (len(to_send) / len(records)) if records else 0.0
    spaces: list[dict] = []
    trail: list[dict] = []
    for record in records:
        answer = answers.get(record["space_id"])
        position_status, position, position_detail = _position(record)
        if answer is None:
            cosmos_detail = "not sent; geometry or rules already settled this slot"
            if record["state"] == "gap":
                cosmos_detail = "not sent; held back so Cosmos stays under 5 percent of slots"
            extra = [
                ("cosmos", cosmos_detail, "", None),
                ("position", position_detail, "", None),
            ]
            evidence = f"{record['video']}#t={record['time_sec']}"
            state = record["state"]
            confidence = record["confidence"]
        else:
            state = _state_for(answer["label"])
            confidence = answer["confidence"]
            extra = [
                (
                    "cosmos",
                    f"model {MODEL} content={answer['raw']!r} label={answer['label']}",
                    "" if answer["label"] != "CANNOT_TELL" else "confidence_below_0.6_or_unparsed",
                    confidence,
                ),
                ("position", position_detail, "", None),
            ]
            evidence = answer["evidence_uri"]
        record["state"] = state
        spaces.append(
            {
                "space_id": record["space_id"],
                "source": "dashcam",
                "video": record["video"],
                "set": record["set"],
                "chunk": str(record["chunk"]),
                "segment_number": str(record["segment_number"]),
                "time_sec": str(record["time_sec"]),
                "side": record["side"],
                "state": state,
                "reason": record["reason"],
                "position": position,
                "position_status": position_status,
                "confidence": "" if confidence is None else f"{float(confidence):.6f}",
                "receipt_text": _receipt_text(record, answer, position_status),
                "evidence_uri": evidence,
                "last_seen": f"{record['set']} segment {record['segment_number']} + {record['time_sec']}s",
            }
        )
        trail.extend(trail_steps(record, extra))
    SPACES.parent.mkdir(parents=True, exist_ok=True)
    with SPACES.open("w") as handle:
        for row in spaces:
            handle.write(json.dumps(row) + "\n")
    with TRAIL.open("w") as handle:
        for row in trail:
            handle.write(json.dumps(row) + "\n")
    try:
        db_status = _write_vastdb(spaces, trail)
    except Exception as exc:  # noqa: BLE001 — JSONL is already on disk
        db_status = f"vastdb insert failed: {exc}"[:500]
    by_set: dict[str, int] = {}
    for row in spaces:
        by_set[row["set"]] = by_set.get(row["set"], 0) + 1
    steps = {row["space_id"]: 0 for row in spaces}
    for row in trail:
        steps[row["space_id"]] = steps.get(row["space_id"], 0) + 1
    incomplete = sum(1 for count in steps.values() if count < 6)
    summary = {
        "slots": len(records),
        "sent": len(to_send),
        "share": round(share, 6),
        "spaces_by_set": by_set,
        "trail_rows": len(trail),
        "incomplete_trails": incomplete,
        "vast": db_status,
    }
    print(json.dumps(summary), file=sys.stderr)
    (ROOT / "dashcam" / "out" / "cosmos_share.json").write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
