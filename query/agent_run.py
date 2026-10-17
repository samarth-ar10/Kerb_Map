"""Second agent: a local LLM (Ollama) that answers kerb questions with the Kerb Map tools.

    python -m query.agent_run --base http://localhost:18001 --model gemma4:e4b
    python -m query.agent_run --base https://<deployed-app> --tasks 1,2,3,4      # read-only tasks

The tools (find_free_space, get_receipt, overrule, plus count_spaces) are the MCP tools of
query/mcp_server.py, re-implemented over the HTTP API so the agent works against any running app.
Writes: overrule is enabled only for the task that asks for it (task 5), at most once, by="agent".
Each task is checked against the data fetched directly from the API (not through the agent), and
the run is written to query/out/agent_run.md.
"""

import argparse
import json
import re
import time
from collections import Counter
from pathlib import Path

import requests

OUT = Path(__file__).parent / "out" / "agent_run.md"
TIMEOUT = 120


class Api:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def get(self, path, **params):
        r = requests.get(self.base + path, params={k: v for k, v in params.items() if v is not None}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def post(self, path, body):
        r = requests.post(self.base + path, json=body, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------- tools (HTTP versions of the MCP tools)

class Tools:
    def __init__(self, api):
        self.api = api
        self.allow_write = False
        self.writes = 0

    def find_free_space(self, lat, lon, radius_m=200.0):
        lat, lon, radius_m = float(lat), float(lon), float(radius_m or 200.0)
        free = self.api.get("/api/spaces", lat=lat, lon=lon, radius_m=radius_m, state="free", limit=5000)["spaces"]
        allsp = self.api.get("/api/spaces", lat=lat, lon=lon, radius_m=radius_m, limit=5000)
        counts = Counter(s.get("state") for s in allsp["spaces"])
        keep = ("space_id", "street", "side", "distance_m", "last_seen", "confidence", "position_status")
        return {"lat": lat, "lon": lon, "radius_m": radius_m, "spaces_seen": allsp["count"],
                "state_counts": dict(counts),
                "free": [{k: s.get(k) for k in keep} for s in free[:5]],
                "note": ("no kerb data near this point: say you cannot tell" if allsp["count"] == 0 else
                         f"no free space within {radius_m:.0f} m: call again with radius_m={radius_m * 2:.0f}"
                         if not free and radius_m < 1000 else None)}

    def get_receipt(self, space_id):
        try:
            d = self.api.get(f"/api/receipt/{space_id}")
        except requests.HTTPError as exc:
            return {"error": str(exc), "space_id": space_id}
        steps = []
        for t in d.get("trail") or []:
            det = t.get("detail_json") or ""
            try:
                det = json.loads(det)
            except (TypeError, ValueError):
                pass
            short = None
            if isinstance(det, dict):
                short = det.get("result") or det.get("state") or det.get("rule")
                if short is None:  # plain values, e.g. n_vehicle_boxes for the boxes step
                    short = ", ".join(f"{k}={v}" for k, v in det.items()
                                      if isinstance(v, (str, int, float, bool)) and len(str(v)) < 60)[:120] or None
            steps.append({"step_no": t.get("step_no"), "step": t.get("step"), "result": short})
        sp = d.get("space") or {}
        return {"space_id": space_id, "state": sp.get("state"), "street": sp.get("street"), "side": sp.get("side"),
                "last_seen": sp.get("last_seen"), "reason": sp.get("reason"),
                "model_state": d.get("model_state"), "evidence_uri": d.get("evidence_uri"),
                "evidence_window_s": [d.get("evidence_start"), d.get("evidence_end")],
                "decision_steps": steps,
                "overrules": [{k: o.get(k) for k in ("overrule_id", "new_state", "by", "note", "at")}
                              for o in d.get("overrules") or []]}

    def count_spaces(self, lat=None, lon=None, radius_m=None, state=None, street=None):
        if lat is None or lon is None:  # whole coverage area (downtown Toronto)
            lat, lon, radius_m = 43.6550, -79.4000, 3000.0
        lat, lon, radius_m = float(lat), float(lon), float(radius_m or 1000.0)
        d = self.api.get("/api/spaces", lat=lat, lon=lon, radius_m=radius_m, state=state or None, limit=5000)
        rows = d["spaces"]
        if street:
            words = [w for w in re.split(r"\W+", street.lower()) if w not in ("st", "ave", "avenue", "street", "")]
            rows = [r for r in rows if all(w in (r.get("street") or "").lower() for w in words)]
        by_side = Counter(r.get("side") for r in rows)
        by_state = Counter(r.get("state") for r in rows)
        by_street = Counter(r.get("street") for r in rows).most_common(5)
        return {"filter": {"lat": lat, "lon": lon, "radius_m": radius_m, "state": state, "street": street},
                "total": len(rows), "by_side": dict(by_side), "by_state": dict(by_state),
                "streets": [s for s, _ in by_street], "truncated_at_5000": d["count"] >= 5000}

    def overrule(self, space_id, state, note):
        if not self.allow_write:
            return {"error": "writes are disabled for this task"}
        if self.writes >= 1:
            return {"error": "only one overrule allowed per run"}
        self.writes += 1
        try:
            d = self.api.post("/api/overrule", {"space_id": space_id, "new_state": state, "note": note, "by": "agent"})
        except requests.HTTPError as exc:
            return {"error": str(exc), "space_id": space_id}
        return {k: d.get(k) for k in ("overrule_id", "space_id", "before", "after")}

    def call(self, name, args):
        fn = {"find_free_space": self.find_free_space, "get_receipt": self.get_receipt,
              "count_spaces": self.count_spaces, "overrule": self.overrule}.get(name)
        if fn is None:
            return {"error": f"unknown tool {name}"}
        try:
            return fn(**args)
        except TypeError as exc:
            return {"error": f"bad arguments: {exc}"}


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


NUM = {"type": "number"}
STR = {"type": "string"}
TOOL_SPECS = [
    _fn("find_free_space", "Free kerb spaces near a point, nearest first, with street and last_seen, plus counts "
        "of every state seen. spaces_seen=0 means there is no data there.",
        {"lat": NUM, "lon": NUM, "radius_m": NUM}, ["lat", "lon"]),
    _fn("get_receipt", "Full receipt for one space_id: state, evidence video, ordered decision steps, overrules.",
        {"space_id": STR}, ["space_id"]),
    _fn("count_spaces", "Count kerb spaces on a street or near a point (lat/lon optional: omit them to "
        "count across all the data), optionally filtered by state "
        "(free|occupied|not_space|cannot_tell) and street name; returns counts by side (L/R) and state.",
        {"lat": NUM, "lon": NUM, "radius_m": NUM, "state": STR, "street": STR}, []),
    _fn("overrule", "Record a correction to a receipt's state (free|occupied|not_space|cannot_tell) with a note.",
        {"space_id": STR, "state": STR, "note": STR}, ["space_id", "state", "note"]),
]

SYSTEM = (
    "You are the Kerb Map agent. Kerb data comes from dashcam drives in downtown Toronto. "
    "Always call a tool before answering, for any place, and never ask the user a question: act. "
    "Never invent spaces, streets, times or counts. "
    "If a tool shows no data for a place (spaces_seen = 0), say you cannot tell. "
    "If no free space is found, you may widen radius_m once (e.g. to 500). "
    "Side L/R is the kerb to the left/right of the camera's direction of travel. "
    "For counts on a street, call count_spaces with street and state. Only call overrule when the user asks you to record a correction. "
    "Answer briefly and cite space_id values."
)


# ---------------------------------------------------------------- agent loop

def chat(ollama, model, messages, tools):
    body = {"model": model, "messages": messages, "tools": tools, "stream": False, "think": False,
            "options": {"temperature": 0}}
    for attempt in range(3):
        r = requests.post(f"{ollama}/api/chat", json=body, timeout=600)
        if r.ok:
            return r.json()["message"]
        print(f"ollama {r.status_code}: {r.text[:200]} (retry {attempt + 1})")
        time.sleep(3)
    r.raise_for_status()


def _json_action(text):
    """Fallback for a model that writes a tool call as JSON text instead of a tool_call."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    name = obj.get("name") or obj.get("tool")
    args = obj.get("arguments") or obj.get("args") or obj.get("parameters") or {}
    return (name, args) if name in {t["function"]["name"] for t in TOOL_SPECS} else None


def summarize(name, res):
    if "error" in res:
        return f"error: {res['error']}"
    if name == "find_free_space":
        f = res["free"]
        head = f"; nearest {f[0]['space_id']} on {f[0]['street']} at {f[0]['distance_m']} m, seen {f[0]['last_seen']}" if f else ""
        return f"{res['spaces_seen']} spaces within {res['radius_m']:.0f} m, states {res['state_counts']}, {len(f)} free shown{head}"
    if name == "get_receipt":
        steps = ", ".join(f"{s['step_no']}.{s['step']}" for s in res["decision_steps"])
        return (f"{res['space_id']}: {res['state']} on {res['street']} ({res['side']}), seen {res['last_seen']}; "
                f"evidence {res['evidence_uri']}; steps {steps}; {len(res['overrules'])} overrule(s)")
    if name == "count_spaces":
        return f"total {res['total']}, by side {res['by_side']}, by state {res['by_state']}, streets {res['streets']}"
    if name == "overrule":
        return f"overrule {res['overrule_id']} on {res['space_id']}: {res['before']} -> {res['after']}"
    return json.dumps(res)[:300]


def run_agent(ollama, model, tools, question, max_steps=6):
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    calls, answer = [], None
    for _ in range(max_steps):
        msg = chat(ollama, model, messages, TOOL_SPECS)
        tcs = msg.get("tool_calls") or []
        if not tcs:
            act = _json_action(msg.get("content"))
            if act:
                tcs = [{"function": {"name": act[0], "arguments": act[1]}}]
                msg = {"role": "assistant", "content": msg.get("content") or ""}
        if not tcs:
            answer = (msg.get("content") or "").strip() or "(empty reply from the model)"
            break
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tcs})
        for tc in tcs:
            name = tc["function"]["name"]
            args = tc["function"].get("arguments") or {}
            if isinstance(args, str):
                args = json.loads(args or "{}")
            res = tools.call(name, args)
            calls.append({"name": name, "args": args, "summary": summarize(name, res), "result": res})
            messages.append({"role": "tool", "content": json.dumps(res, default=str)[:6000], "tool_name": name})
    if answer is None:
        answer = "(no final answer within the step limit)"
    return calls, answer


# ---------------------------------------------------------------- tasks and checks (ground truth fetched directly)

def truth_free(api, lat, lon, radius):
    return api.get("/api/spaces", lat=lat, lon=lon, radius_m=radius, state="free", limit=5000)["spaces"]


def check_task1(api, calls, answer):
    free = truth_free(api, 43.6527, -79.4010, 300)
    if not free:
        return "no free space in the data; answer must say none", "none" in answer.lower()
    n = free[0]
    street_ok = "dundas" in answer.lower() if "Dundas" in (n.get("street") or "") else (n.get("street") or "").lower() in answer.lower()
    time_ok = n["last_seen"][:10] in answer or n["last_seen"][11:16] in answer
    exp = f"{len(free)} free within 300 m; nearest {n['space_id']} on {n['street']} at {n['distance_m']} m, seen {n['last_seen']}"
    return exp, bool(street_ok and time_ok)


def check_task2(api, calls, answer):
    free = []
    for r in (300, 500, 1000):
        free = truth_free(api, 43.6560, -79.3925, r)
        if free:
            break
    n = free[0]
    rec = api.get(f"/api/receipt/{n['space_id']}")
    steps = [t["step"] for t in rec["trail"]]
    got_receipt = any(c["name"] == "get_receipt" and c["args"].get("space_id") == n["space_id"] for c in calls)
    mentions = n["space_id"] in answer and sum(s in answer.lower() for s in steps) >= 2
    exp = f"nearest free {n['space_id']} on {n['street']} at {n['distance_m']} m; steps {steps}; evidence {rec['evidence_uri']}"
    return exp, bool(got_receipt and mentions), n["space_id"]


def check_task3(api, calls, answer):
    rows = api.get("/api/spaces", lat=43.6527, lon=-79.4010, radius_m=2000, state="occupied", limit=5000)["spaces"]
    a = [r for r in rows if "Augusta" in (r.get("street") or "")]
    side = Counter(r["side"] for r in a)
    exp = f"{len(a)} occupied on Augusta Avenue: L {side.get('L', 0)}, R {side.get('R', 0)} (within 2 km, all shown)"
    nums = set(re.findall(r"\d+", answer))
    ok = str(len(a)) in nums and str(side.get("L", 0)) in nums and str(side.get("R", 0)) in nums
    return exp, ok


def check_task4(api, calls, answer):
    rows = api.get("/api/spaces", lat=51.5072, lon=-0.1276, radius_m=2000, limit=50)["spaces"]
    low = answer.lower()
    declined = any(p in low for p in ("cannot tell", "can't tell", "no data", "not able", "unable", "don't have",
                                      "do not have", "no kerb data", "cannot determine", "can't determine",
                                      "no information", "not covered", "outside"))
    invented = bool(re.search(r"\b(dc|pr)_set\d", answer))
    return f"{len(rows)} spaces within 2 km of central London", bool(declined and not invented and not rows)


def check_task5(api, calls, answer, space_id):
    ov = [c for c in calls if c["name"] == "overrule"]
    rec = api.get(f"/api/receipt/{space_id}")
    rows = [o for o in rec["overrules"] if o.get("by") == "agent" and "user report via agent" in (o.get("note") or "")]
    called_ok = len(ov) == 1 and ov[0]["args"].get("space_id") == space_id and ov[0]["args"].get("state") == "occupied"
    stored = rows[-1] if rows else None
    exp = (f"overrule row stored: {stored.get('overrule_id')} new_state={stored.get('new_state')} by={stored.get('by')} "
           f"note={stored.get('note')!r} at {stored.get('at')}; receipt state now {rec['space']['state']}"
           if stored else "no overrule row by=agent found")
    return exp, bool(called_ok and stored and stored.get("new_state") == "occupied")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:18001")
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--model", default="gemma4:e4b")
    ap.add_argument("--tasks", default="1,2,3,4,5")
    a = ap.parse_args()
    api, tools = Api(a.base), Tools(Api(a.base))
    which = {int(x) for x in a.tasks.split(",")}
    health = api.get("/api/health")
    lines = [f"# Second agent run on live data", "",
             f"- Model: {a.model} via Ollama (tool calling through /api/chat `tools`), temperature 0",
             f"- App: {a.base} (backend `{health.get('backend')}`, kerb_spaces {health.get('db_counts', {}).get('kerb_spaces')})",
             f"- Tools: find_free_space, get_receipt, overrule (the MCP tools in query/mcp_server.py, over HTTP), "
             f"plus count_spaces (GET /api/spaces with a street filter)",
             f"- Run at: {time.strftime('%Y-%m-%d %H:%M:%S')} (local)",
             "- Check: each answer is compared with data fetched straight from the API, not through the agent.", ""]
    results = {}
    t2_id = t1_id = None

    def record(n, q, calls, answer, exp, ok, secs):
        results[n] = ok
        lines.extend([f"## Task {n}: {'PASS' if ok else 'FAIL'}", "", f"**Question.** {q}", "", "**Tool calls.**", ""])
        if not calls:
            lines.append("- (none)")
        for i, c in enumerate(calls, 1):
            lines.append(f"{i}. `{c['name']}({json.dumps(c['args'])})` -> {c['summary']}")
        lines.extend(["", f"**Final answer.** {answer}", "", f"**Check against data.** {exp}", "",
                      f"_({secs:.0f} s)_", ""])
        print(f"task {n}: {'PASS' if ok else 'FAIL'}  ({secs:.0f}s)")

    tasks = {
        1: "Is there a free kerb space within 300 m of Dundas St W & Augusta Ave (43.6527, -79.4010)? "
           "Give the street and when it was seen.",
        2: "Show me the evidence and decision steps for the nearest free space to McCaul St & Baldwin St "
           "(43.6560, -79.3925).",
        3: "How many occupied kerb spots were seen on Augusta Ave, and on which side?",
        4: "Is there free parking at 51.5072, -0.1276 (central London) right now?",
    }
    for n in (1, 2, 3, 4):
        if n not in which:
            continue
        t0 = time.time()
        tools.allow_write = False
        calls, answer = run_agent(a.ollama, a.model, tools, tasks[n])
        if n == 1:
            exp, ok = check_task1(api, calls, answer)
            f = truth_free(api, 43.6527, -79.4010, 300)
            t1_id = f[0]["space_id"] if f else None
        elif n == 2:
            exp, ok, t2_id = check_task2(api, calls, answer)
        elif n == 3:
            exp, ok = check_task3(api, calls, answer)
        else:
            exp, ok = check_task4(api, calls, answer)
        record(n, tasks[n], calls, answer, exp, ok, time.time() - t0)

    if 5 in which:
        sid = t1_id or truth_free(api, 43.6527, -79.4010, 300)[0]["space_id"]
        q = (f"A user says receipt {sid} is wrong because a car is parked there now. Record that. "
             f"Use the note \"user report via agent\".")
        t0 = time.time()
        tools.allow_write = True
        calls, answer = run_agent(a.ollama, a.model, tools, q)
        tools.allow_write = False
        exp, ok = check_task5(api, calls, answer, sid)
        record(5, q, calls, answer, exp, ok, time.time() - t0)

    passed = sum(results.values())
    lines[7:7] = [f"**Result: {passed}/{len(results)} tasks pass.**", ""]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"{passed}/{len(results)} pass -> {OUT}")


if __name__ == "__main__":
    main()
