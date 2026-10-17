"""MCP tools for a second agent: find_free_space, get_receipt, overrule (stdio transport).

    KERB_DB=local python -m query.mcp_server        # from the repo root

Uses the same data path as the API (database + builders' JSONL files). "cannot_tell" and
"position uncertain" are returned as they are, so the agent can abstain instead of guessing.
"""

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server

from common.kerb_schema import SPACE_STATES
from query import api, store

mcp = Server("kerb-map")


@mcp.tool()
def find_free_space(lat: float, lon: float, radius_m: float = 200.0) -> dict:
    """Kerb spaces near a point. Returns free spaces nearest first, plus counts of every state seen,
    so 'nothing free' and 'cannot tell' can be told apart."""
    rows = store.query_spaces(lat, lon, radius_m, limit=500)
    counts = {s: 0 for s in SPACE_STATES}
    for r in rows:
        counts[r.get("state")] = counts.get(r.get("state"), 0) + 1
    free = [store.public(r) for r in rows if r.get("state") == "free"]
    keep = ("space_id", "state", "side", "source", "street", "distance_m", "confidence", "position_status",
            "last_seen", "receipt_text", "is_test")
    return {"lat": lat, "lon": lon, "radius_m": radius_m, "spaces_seen": len(rows), "state_counts": counts,
            "free": [{k: r.get(k) for k in keep} for r in free[:20]]}


@mcp.tool()
def get_receipt(space_id: str) -> dict:
    """Full receipt for one space: state, evidence_uri, ordered decision trail, overrules."""
    try:
        out = api.receipt(space_id)
    except api.HTTPException as exc:
        return {"error": exc.detail, "space_id": space_id}
    return {k: out[k] for k in ("space", "model_state", "trail", "overrules", "evidence_uri", "camera")}


@mcp.tool()
def overrule(space_id: str, state: str, note: str, by: str = "agent") -> dict:
    """Correct a receipt's state (free | occupied | not_space | cannot_tell). Returns before/after."""
    try:
        out = api.overrule(api.OverruleIn(space_id=space_id, new_state=state, note=note, by=by))
    except api.HTTPException as exc:
        return {"error": exc.detail, "space_id": space_id}
    return {k: out[k] for k in ("overrule_id", "space_id", "before", "after")}


if __name__ == "__main__":
    mcp.run()
