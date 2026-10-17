"""A drive's own log: streets and receipt counts from rows already stored.

Read-only. A drive is one camera, and for the dashcam one PIE set, the same key
replay uses (for example pie_cam-3:set01). Counts come only from kerb_spaces
whose original_video is one of that drive's clips.
"""

from collections import Counter

from common.kerb_schema import SPACE_STATES

NOTE = "Log of receipts already made from the supplied footage. Nothing new is uploaded."


def _median(values):
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return None
    mid = count // 2
    if count % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def summarize(drive_id, videos, spaces):
    """videos: original_video values for this drive. spaces: kerb_spaces rows."""
    wanted = set(videos)
    matched = [row for row in spaces if row.get("original_video") in wanted]
    counts = Counter((row.get("state") or "unknown") for row in matched)
    by_street = {}
    for row in matched:
        name = (row.get("street") or "").strip() or "Street unknown"
        by_street.setdefault(name, Counter())[row.get("state") or "unknown"] += 1
    streets = []
    for name, bucket in by_street.items():
        streets.append({
            "street": name,
            "receipts": sum(bucket.values()),
            "free": bucket.get("free", 0),
            "occupied": bucket.get("occupied", 0),
            "cannot_tell": bucket.get("cannot_tell", 0),
            "not_space": bucket.get("not_space", 0),
        })
    streets.sort(key=lambda item: (-item["receipts"], item["street"]))
    pins = [
        (row["lat"], row["lon"]) for row in matched
        if row.get("lat") is not None and row.get("lon") is not None and row.get("position_status") == "ok"
    ]
    last_seen = max((row.get("last_seen") or "" for row in matched), default="") or None
    return {
        "drive_id": drive_id,
        "clips": len(videos),
        "receipts": len(matched),
        "counts": {state: counts.get(state, 0) for state in SPACE_STATES},
        "other": sum(value for key, value in counts.items() if key not in SPACE_STATES),
        "streets": streets,
        "pins": len(pins),
        "position_uncertain": sum(
            1 for row in matched
            if row.get("position_status") != "ok" or row.get("lat") is None or row.get("lon") is None
        ),
        "lat": None if not pins else _median([pair[0] for pair in pins]),
        "lon": None if not pins else _median([pair[1] for pair in pins]),
        "last_seen": last_seen,
        "test_rows": sum(1 for row in matched if str(row.get("receipt_text") or "").startswith("TEST:")),
        "note": NOTE,
    }
