"""Insert a few TEST rows into the LOCAL backend so the API and pages can be exercised.

    KERB_DB=local python -m query.seed_local          # from the repo root

These rows are made up for testing the plumbing. Every space_id starts with "TEST_" and every
receipt_text starts with "TEST:"; the pages show a TEST badge on them. They are never results.
The script refuses to run against VastDB.
"""

import json
import os
import sys

from common import kerbdb

# Around Vaughan Road, Toronto (the first PIE drive), only so pins land somewhere sensible.
SPACES = [
    ("TEST_001", "free", "L", 43.68296, -79.42049, "TEST: made-up free gap between two parked cars"),
    ("TEST_002", "occupied", "L", 43.68280, -79.42020, "TEST: made-up parked car on the kerb"),
    ("TEST_003", "not_space", "R", 43.68262, -79.41990, "TEST: made-up driveway, not a space"),
    ("TEST_004", "cannot_tell", "R", 43.68240, -79.41950, "TEST: made-up unclear gap, view blocked by a van"),
    ("TEST_005", "free", "view", None, None, "TEST: made-up street camera view, placed at its camera"),
]

TEST_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080">
<rect width="1920" height="1080" fill="#d9d9d9"/>
<text x="960" y="540" font-family="sans-serif" font-size="120" text-anchor="middle" fill="#555">TEST FRAME</text>
<text x="960" y="680" font-family="sans-serif" font-size="48" text-anchor="middle" fill="#555">not real evidence</text>
</svg>"""


def main():
    db = kerbdb.get_db()
    if db.backend != "local":
        sys.exit("seed_local only writes to the local backend; set KERB_DB=local")
    db.ensure_tables()

    frame_dir = os.path.expanduser("~/kerb-data/test_evidence")
    os.makedirs(frame_dir, exist_ok=True)
    frame = os.path.join(frame_dir, "TEST_frame.svg")
    with open(frame, "w") as fh:
        fh.write(TEST_SVG)

    if db.read("kerb_spaces", {"space_id": "TEST_001"}, limit=1):
        print("TEST rows already present, nothing to do")
        return

    db.insert("kerb_cameras", [{"camera_id": "TEST_cam", "kind": "street_camera", "lat": 43.68320,
                                "lon": -79.42100, "heading": 90.0, "place": "TEST camera",
                                "geo_source": "unknown", "geo_confidence": None, "check_error_m": None}])
    for i, (sid, state, side, lat, lon, text) in enumerate(SPACES):
        source = "streetcam" if side == "view" else "dashcam"
        db.upsert_space({
            "space_id": sid, "source": source, "camera_id": "TEST_cam" if source == "streetcam" else "pie_cam-3",
            "original_video": "TEST_video.mp4", "segment_number": 1, "t_sec": float(i * 5), "side": side,
            "state": state, "reason": "TEST reason", "confidence": 0.5, "lat": lat, "lon": lon,
            "position_status": "ok" if lat is not None else "uncertain", "street": "TEST street",
            "length_m": 6.0, "last_seen": "2026-10-17T12:00:00Z", "evidence_uri": frame, "receipt_text": text,
            "embedding": None,
        })
        db.insert("kerb_trail", [
            {"trail_id": f"{sid}_1", "space_id": sid, "step_no": 1, "step": "gate",
             "detail_json": json.dumps({"test": True, "note": "TEST gate step"}), "at": "2026-10-17T12:00:00Z"},
            {"trail_id": f"{sid}_2", "space_id": sid, "step_no": 2, "step": "boxes",
             "detail_json": json.dumps({"test": True, "box": [700, 500, 1100, 800], "frame_size": [1920, 1080]}),
             "at": "2026-10-17T12:00:01Z"},
            {"trail_id": f"{sid}_3", "space_id": sid, "step_no": 3, "step": "rule",
             "detail_json": json.dumps({"test": True, "note": "TEST rule step"}), "at": "2026-10-17T12:00:02Z"},
        ])
        db.insert("kerb_observations", [{"obs_id": f"{sid}_obs1", "space_id": sid, "source": source,
                                         "camera_id": "TEST_cam", "segment_source": "TEST_segment.mp4",
                                         "t_sec": float(i * 5), "state": state, "confidence": 0.5}])
    print(f"inserted {len(SPACES)} TEST spaces into {db.path}")


if __name__ == "__main__":
    main()
