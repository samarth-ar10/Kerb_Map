"""Second agent: five questions from query/agent_eval.md, answered with the MCP tools.

The model may only call find_free_space, get_receipt and overrule. The run writes a local
SQLite file and never talks to VastDB. An overrule lands in that local file.

    KERB_DB=local python -m query.second_agent
    AGENT_LLM_URL=http://127.0.0.1:8851/v1 python -m query.second_agent

The transcript is query/out/agent_transcript.json. Pass or fail follows agent_eval.md.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

from common.kerbdb import haversine_m
from query.sources import JsonlSource

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "query" / "out" / "agent_transcript.json"
VAUGHAN = (43.68296, -79.42049)
TASK1_RADIUS = 150.0
TASK5_RADIUS = 300.0

SYSTEM = """You are a Kerb Map agent. Reply with one JSON object and no other text.
Either call a tool: {"tool":"find_free_space","args":{"lat":0,"lon":0,"radius_m":150}}
or {"tool":"get_receipt","args":{"space_id":"..."}}
or {"tool":"overrule","args":{"space_id":"...","state":"not_space","note":"...","by":"agent"}}
or finish: {"answer":"..."}
Rules:
- Use the radius written in the question. Do not change it.
- Cite only values the tool returned. Do not invent a space id, a distance, or a trail step.
- If the tool returns free spaces, call get_receipt on the nearest one, then cite its space_id, distance_m, confidence, last_seen and state.
- If spaces_seen is 0, say nothing has been seen there. Unknown is not the same as empty. Do not call overrule.
- If receipts were seen and every state is cannot_tell, say cannot tell and name that state. Do not say there are no free spaces.
- If receipts were seen, none are free, and some are occupied or not_space, say no free space was returned.
- A driveway correction calls overrule with state not_space and a note that contains the word driveway, then reports before and after from the tool.
- If trail is empty, say there are no trail steps. Mention evidence_uri when the tool returned one.
"""


def load_spaces():
    src = JsonlSource()
    rows = src.read("kerb_spaces", None, limit=10**9)
    return [r for r in rows if not str(r.get("receipt_text") or "").startswith("TEST:")
            and r.get("lat") is not None and r.get("lon") is not None]


def _within(spaces, lat, lon, radius_m):
    return [r for r in spaces if haversine_m(lat, lon, r["lat"], r["lon"]) <= radius_m]


def prepare_tasks(spaces=None):
    """Fill the five question blanks from the receipt files. No model is involved."""
    spaces = load_spaces() if spaces is None else spaces
    dash = sorted(
        (r for r in spaces if r.get("source") == "dashcam" and r.get("evidence_uri")),
        key=lambda r: r["space_id"],
    )
    if not dash:
        raise SystemExit("no dashcam receipt with evidence_uri and coordinates")
    drive = sorted(
        (r for r in spaces if str(r.get("reason") or "").startswith("near driveway")),
        key=lambda r: r["space_id"],
    )
    if not drive:
        raise SystemExit("no receipt whose reason starts with 'near driveway'")

    abstain = None
    best = None
    for centre in spaces:
        if centre.get("state") != "cannot_tell":
            continue
        near = _within(spaces, centre["lat"], centre["lon"], 100.0)
        if len(near) < 3:
            continue
        if not all(r.get("state") == "cannot_tell" for r in near):
            continue
        rank = (-len(near), centre["space_id"])
        if best is None or rank < best[0]:
            best = (rank, centre, near)
    if best is None:
        raise SystemExit("no pin where every receipt within 100 m is cannot_tell")
    abstain = best[1]

    lon = sorted(r["lon"] for r in spaces)[len(spaces) // 2]
    lat = max(r["lat"] for r in spaces)
    gap = 0.01
    while True:
        pin = (lat + gap, lon)
        dist = min(haversine_m(pin[0], pin[1], r["lat"], r["lon"]) for r in spaces)
        if dist > TASK5_RADIUS + 50:
            break
        gap += 0.002
        if gap > 1:
            raise SystemExit("could not find a pin with nothing within 300 m")

    evidence = dash[0]
    driveway = drive[0]
    return [
        {
            "n": 1,
            "question": (
                f"Is there a free space within {TASK1_RADIUS:.0f} m of "
                f"{VAUGHAN[0]}, {VAUGHAN[1]} (Vaughan Road)? "
                "Give the nearest one and why you trust it."
            ),
            "lat": VAUGHAN[0], "lon": VAUGHAN[1], "radius_m": TASK1_RADIUS,
        },
        {
            "n": 2,
            "question": (
                f"Show me the evidence for space {evidence['space_id']}: "
                "what did each step decide?"
            ),
            "space_id": evidence["space_id"],
        },
        {
            "n": 3,
            "question": (
                f"A driver reports that {driveway['space_id']} is a driveway. Correct it."
            ),
            "space_id": driveway["space_id"],
        },
        {
            "n": 4,
            "question": (
                f"Is there a free space near {abstain['lat']}, {abstain['lon']} "
                "within 100 m?"
            ),
            "lat": abstain["lat"], "lon": abstain["lon"], "radius_m": 100.0,
        },
        {
            "n": 5,
            "question": (
                f"Find a free space near {pin[0]:.5f}, {pin[1]:.5f} within 300 m."
            ),
            "lat": pin[0], "lon": pin[1], "radius_m": TASK5_RADIUS,
            "min_distance_m": dist,
        },
    ]


def _contains(answer, value):
    if value is None:
        return False
    text = answer.lower()
    raw = str(value).lower()
    if raw in text:
        return True
    if isinstance(value, float):
        return f"{value:.2f}" in text or f"{value:.1f}" in text or f"{value:.0f}" in text
    return False


def _ids_in(answer):
    return set(re.findall(r"\b(?:dc_|pr_|set\d|neighborhood_|sf_|nyc_)[\w.-]+", answer))


def score_task(task, calls, answer):
    """Return (passed, why) using the rules in query/agent_eval.md."""
    answer = answer or ""
    names = [c["tool"] for c in calls]
    find = [c for c in calls if c["tool"] == "find_free_space"]
    got = [c for c in calls if c["tool"] == "get_receipt"]
    over = [c for c in calls if c["tool"] == "overrule"]
    n = task["n"]

    if n == 1:
        if not find:
            return False, "did not call find_free_space"
        free = (find[-1].get("result") or {}).get("free") or []
        if not free:
            return False, "find_free_space returned no free space, so the written pass rule cannot be met"
        named = _ids_in(answer)
        match = [r for r in free if r.get("space_id") in named]
        if not match:
            return False, "answer does not cite a free space_id the tool returned"
        space = match[0]
        if not any(c["args"].get("space_id") == space["space_id"] for c in got):
            return False, "did not call get_receipt on the space it named"
        receipt = next(c for c in got if c["args"].get("space_id") == space["space_id"])
        state = ((receipt.get("result") or {}).get("space") or {}).get("state")
        if state and state not in answer.lower() and state.replace("_", " ") not in answer.lower():
            return False, "answer state does not match the receipt"
        for field in ("distance_m", "confidence", "last_seen"):
            if not _contains(answer, space.get(field)):
                return False, f"answer does not cite {field}"
        return True, "cited the tool's free space"

    if n == 2:
        if not any(c["args"].get("space_id") == task["space_id"] for c in got):
            return False, "did not call get_receipt on the asked space"
        receipt = next(c for c in got if c["args"].get("space_id") == task["space_id"])
        result = receipt.get("result") or {}
        trail = result.get("trail") or []
        steps = [t.get("step") for t in sorted(trail, key=lambda t: t.get("step_no") or 0)]
        if any(step and step not in answer for step in steps):
            return False, "answer misses a trail step the tool returned"
        blob = json.dumps(result).lower()
        invented = [w for w in ("gate", "boxes", "geometry", "rule", "cosmos", "track", "position", "overrule")
                    if w in answer.lower() and w not in steps and w not in blob]
        if invented:
            return False, "answer names a trail step the tool did not return: " + ", ".join(invented)
        ev = result.get("evidence_uri") or ""
        if ev and ev not in answer:
            return False, "answer does not mention evidence_uri"
        return True, "trail matches the tool"

    if n == 3:
        good = [c for c in over if c["args"].get("space_id") == task["space_id"]
                and c["args"].get("state") == "not_space"
                and "driveway" in str(c["args"].get("note") or "").lower()]
        if not good:
            return False, "did not overrule that space to not_space with a driveway note"
        result = good[-1].get("result") or {}
        before = str((result.get("before") or {}).get("state") or "")
        after = str((result.get("after") or {}).get("state") or "")
        if before and before not in answer:
            return False, "answer does not report the before state"
        if after and after not in answer:
            return False, "answer does not report the after state"
        return True, "overrule before and after reported"

    if n == 4:
        if not find:
            return False, "did not call find_free_space"
        if over:
            return False, "called overrule"
        if re.search(r"no free|nothing free|aren't any free|are no free", answer.lower()):
            return False, "said there are no free spaces"
        if _ids_in(answer):
            return False, "named a space"
        if not re.search(r"cannot tell|can't tell|not enough evidence", answer.lower()):
            return False, "did not say cannot tell"
        counts = (find[-1].get("result") or {}).get("state_counts") or {}
        seen = [k for k, v in counts.items() if v]
        if seen and any(k != "cannot_tell" for k in seen):
            return False, "find_free_space saw a state other than cannot_tell"
        return True, "abstained"

    if n == 5:
        if not find:
            return False, "did not call find_free_space"
        if over:
            return False, "called overrule"
        seen = (find[-1].get("result") or {}).get("spaces_seen")
        if seen:
            return False, "tool saw receipts; this pin is not empty"
        if _ids_in(answer):
            return False, "invented a space id"
        if not re.search(r"nothing has been seen|not been seen|has not been seen|no receipts|nothing was seen|unknown", answer.lower()):
            return False, "did not say the place has not been seen"
        if re.search(r"\bempty kerb\b|\bno cars parked\b|\bit is empty\b", answer.lower()):
            return False, "treated unseen as empty"
        return True, "said unseen, not empty"

    return False, "unknown task"


def parse_reply(text):
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _post(url, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def complete(base_url, model, messages):
    body = {"model": model, "messages": messages, "temperature": 0, "max_tokens": 500}
    out = _post(base_url.rstrip("/") + "/chat/completions", body)
    return out["choices"][0]["message"]["content"]


def model_id(base_url):
    out = _post_get(base_url.rstrip("/") + "/models")
    data = out.get("data") or []
    if not data:
        return "local"
    return data[0].get("id") or "local"


def _post_get(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode())


def run_task(base_url, model, tools, task):
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": task["question"]},
    ]
    calls = []
    answer = ""
    for _ in range(5):
        reply = complete(base_url, model, messages)
        obj = parse_reply(reply)
        messages.append({"role": "assistant", "content": reply})
        if not obj:
            messages.append({"role": "user", "content": "Reply with one JSON object, either a tool call or {\"answer\": \"...\"}."})
            continue
        if obj.get("answer"):
            answer = str(obj["answer"])
            break
        name = obj.get("tool")
        args = obj.get("args") or {}
        if name not in tools:
            messages.append({"role": "user", "content": json.dumps({"error": "unknown tool"})})
            continue
        try:
            result = tools[name](**_filter_args(name, args))
        except TypeError as exc:
            result = {"error": str(exc)}
        calls.append({"tool": name, "args": _filter_args(name, args), "result": result})
        messages.append({"role": "user", "content": json.dumps({"result": result}) + (
            "\nIf you have what you need, reply with {\"answer\": \"...\"}. "
            "Copy evidence_uri, state, distance_m, confidence and last_seen when the result includes them. "
            "If every counted state is cannot_tell, say cannot tell. "
            "If spaces_seen is 0, say nothing has been seen there."
        )})
    else:
        answer = answer or ""
    passed, why = score_task(task, calls, answer)
    return {"n": task["n"], "question": task["question"], "calls": calls, "answer": answer,
            "pass": passed, "why": why}


def _filter_args(name, args):
    args = args or {}
    if name == "find_free_space":
        out = {}
        for key in ("lat", "lon", "radius_m"):
            if key in args and args[key] is not None:
                out[key] = float(args[key])
        return out
    if name == "get_receipt":
        return {"space_id": str(args["space_id"])} if args.get("space_id") else {}
    if name == "overrule":
        out = {k: args[k] for k in ("space_id", "state", "note", "by") if args.get(k) is not None}
        if "space_id" in out:
            out["space_id"] = str(out["space_id"])
        return out
    return {}


def tools_from_mcp():
    from query import mcp_server
    return {
        "find_free_space": mcp_server.find_free_space,
        "get_receipt": mcp_server.get_receipt,
        "overrule": mcp_server.overrule,
    }


def main():
    if os.environ.get("KERB_DB") == "vastdb":
        raise SystemExit("Refusing KERB_DB=vastdb. This run writes one local overrule only.")
    os.environ["KERB_DB"] = "local"
    os.environ.setdefault("KERB_SQLITE", "/tmp/kerb-agent/kerb.sqlite")
    Path(os.environ["KERB_SQLITE"]).parent.mkdir(parents=True, exist_ok=True)
    base = os.environ.get("AGENT_LLM_URL", "http://127.0.0.1:8851/v1")
    model = os.environ.get("AGENT_MODEL") or model_id(base)
    model_name = Path(model).name or model
    tasks = prepare_tasks()
    tools = tools_from_mcp()
    done = [run_task(base, model, tools, task) for task in tasks]
    passed = sum(1 for t in done if t["pass"])
    transcript = {"model": model_name, "llm_url": base, "backend": "local", "passed": passed, "of": len(done),
                  "tasks": done}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(transcript, indent=2) + "\n")
    print(json.dumps({"passed": passed, "of": len(done), "why": [t["why"] for t in done], "path": str(OUT)}))


if __name__ == "__main__":
    main()
