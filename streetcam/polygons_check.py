#!/usr/bin/env python3
"""Draw kerb_neighborhood.json on 3 day and 3 night frames -> out/polygons_check.jpg.

Frames: frame_neighborhood.jpg (20260901, t=14.0 s) plus the full-scene
pictures embedded in label batches A3/A6 (1100x620, rescaled to 2048x1154;
those pictures already carry the OLD translucent bands, the new areas are
drawn as thick outlines). On each frame every vehicle box bottom centre at
that frame time is drawn as a dot, coloured by the area it falls in.

    python streetcam/polygons_check.py --labels-dir <path>/day-of/label-batches
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path

import cv2
import numpy as np

from parked import area_of, bottom_center

HERE = Path(__file__).resolve().parent
W, H = 2048, 1154
# (label, source, day, time_s) ; source is "frame" or a label item id
PANELS = [
    ("day 20260901 t=14.0", "frame", "20260901", 14.0),
    ("day 20260901 t=109.6", "nc1_20260901-0028", "20260901", 109.6),
    ("day 20260901 t=133.45", "nc1_20260901-0022", "20260901", 133.45),
    ("night 20260902 t=174.0", "nc1_20260902-0149", "20260902", 174.0),
    ("night 20260902 t=232.7", "nc1_20260902-0204", "20260902", 232.7),
    ("night 20260902 t=556.3", "nc1_20260902-0326", "20260902", 556.3),
]
COL = {"near": (0, 230, 255), "far": (60, 220, 60), "lane": (255, 120, 40),
       "driveway": (40, 40, 255), "off_street": (200, 200, 200)}
VEHICLES = {"car", "truck", "bus", "motorcycle"}


def label_images(labels_dir: Path, wanted):
    found = {}
    for f in sorted(labels_dir.glob("label_batch_A[36].html")):
        s = f.read_text(encoding="utf-8")
        m = re.search(r'<script id="data" type="application/json">(.*?)</script>', s, re.S)
        for it in json.loads(m.group(1)):
            if it["item_id"] in wanted and it["item_id"] not in found:
                buf = np.frombuffer(base64.b64decode(it["img"]), np.uint8)
                found[it["item_id"]] = cv2.resize(cv2.imdecode(buf, cv2.IMREAD_COLOR), (W, H))
    return found


def boxes_at(day, t):
    out = []
    with (HERE / f"day-{day}" / "tracks.jsonl").open() as fh:
        for line in fh:
            r = json.loads(line)
            if r["label"] not in VEHICLES or not (r["start_time_s"] - 0.03 <= t <= r["end_time_s"] + 0.03):
                continue
            for fr, b in zip(r["frames"], r["box_path"]):
                if abs(fr["time_s"] - t) < 0.026:
                    out.append(b)
    return out


def draw(img, kerb, title, boxes):
    def poly(pts, col, name, thick=5):
        p = np.array(pts, np.int32)
        cv2.polylines(img, [p], True, col, thick, cv2.LINE_AA)
        x, y = p.min(axis=0)
        cv2.putText(img, name, (int(x) + 6, int(y) + 28), 0, 0.9, col, 2, cv2.LINE_AA)

    poly(kerb["lane"]["polygon"], COL["lane"], "lane", 4)
    for s in kerb["kerb_strips"]:
        poly(s["polygon"], COL[s["id"]], s["id"])
    for d in kerb["driveways"]:
        poly(d["polygon"], COL["driveway"], "drive " + d["id"])
    for b in boxes:
        x, y = bottom_center(b)
        kind, aid = area_of(x, y, kerb)
        col = COL[aid] if kind == "kerb" else COL[kind]
        cv2.rectangle(img, (int(b[0]), int(b[1])), (int(b[2]), int(b[3])), (255, 255, 255), 2)
        cv2.circle(img, (int(x), int(y)), 11, (0, 0, 0), -1)
        cv2.circle(img, (int(x), int(y)), 8, col, -1)
    cv2.rectangle(img, (0, 0), (W, 60), (0, 0, 0), -1)
    cv2.putText(img, f"{title}  boxes: {len(boxes)}", (14, 44), 0, 1.4, (255, 255, 255), 3, cv2.LINE_AA)
    return img


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--labels-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, default=HERE / "out" / "polygons_check.jpg")
    args = p.parse_args()
    kerb = json.loads((HERE / "kerb_neighborhood.json").read_text())
    imgs = label_images(args.labels_dir, {src for _, src, _, _ in PANELS if src != "frame"})
    tiles = []
    for title, src, day, t in PANELS:
        img = cv2.imread(str(HERE / "frame_neighborhood.jpg")) if src == "frame" else imgs[src].copy()
        tiles.append(cv2.resize(draw(img, kerb, title, boxes_at(day, t)), (1024, 577)))
    sheet = np.vstack([np.hstack(tiles[:3]), np.hstack(tiles[3:])])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(args.out), sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
    print(args.out)


if __name__ == "__main__":
    main()
