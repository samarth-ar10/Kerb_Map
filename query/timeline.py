"""Chunk timeline for replay (stdlib only, so the deploy build can use it without the app's deps)."""

import csv
import os
import re


def _drive_key(camera_id, filename):
    m = re.search(r"_(set\d\d)_", filename or "")
    return f"{camera_id}:{m.group(1)}" if m else camera_id


def load_chunks(path):
    """[(drive, chunk_filename, original_video)] unique per chunk, from any of the timeline files."""
    out = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            video = row.get("original_video")
            cam = row.get("camera_id")
            if not video or not cam:
                continue
            name = row.get("chunk_filename") or row.get("filename") or os.path.basename(video)
            out[video] = (row.get("drive") or _drive_key(cam, name), name, video)
    return sorted(out.values(), key=lambda r: (r[0], r[1]))
