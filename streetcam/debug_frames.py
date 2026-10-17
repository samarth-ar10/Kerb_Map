#!/usr/bin/env python3
"""Draw track ids on three frames and save one JPEG.

Picks three frames from day 20260901 that contain at least two tracks,
spread across the day. Downloads those segments with the retrieval stream
URL. The token is read from ~/kerb-work and is not printed.
"""

from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path("/home/psramanuj/wt/streetcam/streetcam")
DET = Path("/home/psramanuj/kerb-work/dets/20260901")
DAY = "20260901"

# 5x7 digits, row-major, 1 = ink.
FONT = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "10000", "11110", "00001", "00001", "11110"],
    "6": ["01110", "10000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
}


def load_auth():
    home = Path.home() / "kerb-work"
    return (home / "backend").read_text().strip().rstrip("/"), (home / "token").read_text().strip()


def pick_frames():
    buckets = defaultdict(list)  # (chunk, seg, frame) -> list of (track_id, box)
    with (ROOT / "day-20260901" / "tracks.jsonl").open() as fh:
        for line in fh:
            rec = json.loads(line)
            for fr, box in zip(rec["frames"], rec["box_path"]):
                key = (fr["chunk_index"], fr["segment_number"], fr["frame_index"], fr["time_s"])
                buckets[key].append((rec["track_id"], box))
    # one candidate per chunk: the frame in that chunk with the most tracks
    per_chunk = {}
    for key, items in buckets.items():
        ci = key[0]
        cur = per_chunk.get(ci)
        if cur is None or len(items) > len(cur[1]):
            per_chunk[ci] = (key, items)
    ranked = sorted(per_chunk.items())
    if len(ranked) < 3:
        raise SystemExit(f"need 3 chunks with tracks, found {len(ranked)}")
    chosen_idx = [0, len(ranked) // 2, len(ranked) - 1]
    return [ranked[i][1] for i in chosen_idx]


def download_segment(source: str, dest: Path):
    if dest.exists() and dest.stat().st_size > 1000:
        return
    backend, token = load_auth()
    q = urllib.parse.urlencode({"source": source, "token": token})
    url = f"{backend}/api/v1/videos/stream?{q}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=180) as resp, dest.open("wb") as fh:
        while True:
            block = resp.read(1 << 16)
            if not block:
                break
            fh.write(block)


def extract_rgb(video: Path, time_s: float):
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{time_s:.3f}", "-i", str(video),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    # probe size
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video),
        ],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    w, h = [int(x) for x in probe.stdout.strip().split(",")]
    expected = w * h * 3
    buf = proc.stdout
    if len(buf) < expected:
        raise SystemExit(f"short frame {len(buf)} != {expected}")
    return w, h, bytearray(buf[:expected])


def draw_rect(buf, w, h, box, color):
    x1, y1, x2, y2 = [int(round(v)) for v in box]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w - 1, x2), min(h - 1, y2)
    def pix(x, y):
        i = (y * w + x) * 3
        buf[i:i + 3] = bytes(color)
    for t in range(3):
        yy1, yy2 = min(h - 1, y1 + t), max(0, y2 - t)
        xx1, xx2 = min(w - 1, x1 + t), max(0, x2 - t)
        for x in range(x1, x2 + 1):
            if 0 <= y1 + t < h:
                pix(x, y1 + t)
            if 0 <= y2 - t < h:
                pix(x, y2 - t)
        for y in range(y1, y2 + 1):
            if 0 <= x1 + t < w:
                pix(x1 + t, y)
            if 0 <= x2 - t < w:
                pix(x2 - t, y)


def draw_text(buf, w, h, x, y, text, scale=3):
    color = (255, 255, 0)
    tw, th = 6 * scale * len(text), 7 * scale
    x, y = max(0, x), max(0, y)
    for py in range(y, min(h, y + th)):
        for px in range(x, min(w, x + tw)):
            i = (py * w + px) * 3
            buf[i:i + 3] = b"\x00\x00\x00"
    cx = x
    for ch in text:
        glyph = FONT.get(ch)
        if glyph is None:
            cx += 6 * scale
            continue
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit != "1":
                    continue
                for sy in range(scale):
                    for sx in range(scale):
                        px, py = cx + gx * scale + sx, y + gy * scale + sy
                        if 0 <= px < w and 0 <= py < h:
                            i = (py * w + px) * 3
                            buf[i:i + 3] = bytes(color)
        cx += 6 * scale


def resize(buf, w, h, nw, nh):
    out = bytearray(nw * nh * 3)
    for y in range(nh):
        sy = min(h - 1, y * h // nh)
        row = sy * w
        for x in range(nw):
            sx = min(w - 1, x * w // nw)
            si = (row + sx) * 3
            di = (y * nw + x) * 3
            out[di:di + 3] = buf[si:si + 3]
    return out


def to_jpeg(buf, w, h, dest: Path):
    ppm = dest.with_suffix(".ppm")
    with ppm.open("wb") as fh:
        fh.write(f"P6\n{w} {h}\n255\n".encode())
        fh.write(buf)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(ppm), str(dest)],
        check=True,
    )
    ppm.unlink()


def main():
    index = json.loads((DET / "index.json").read_text())
    by_chunk = {c["chunk_index"]: c for c in index["chunks"]}
    chosen = pick_frames()
    panels = []
    notes = []
    tmp = Path("/tmp/streetcam-frames")
    tmp.mkdir(exist_ok=True)
    for key, items in chosen:
        chunk_index, segment_number, frame_index, time_s = key
        chunk = by_chunk[chunk_index]
        seg = next(s for s in chunk["segments"] if s["segment_number"] == segment_number)
        raw = json.loads((DET / seg["file"]).read_text())
        frame = next(f for f in raw["frames"] if int(f["frame_index"]) == frame_index)
        video = tmp / f"c{chunk_index:04d}_s{segment_number:03d}.mp4"
        download_segment(seg["source"], video)
        w, h, buf = extract_rgb(video, float(frame["time_sec"]))
        shape = frame.get("shape") or []
        sx = w / shape[1] if len(shape) == 2 and shape[1] else 1
        sy = h / shape[0] if len(shape) == 2 and shape[0] else 1
        nw, nh = 640, 360
        small = resize(buf, w, h, nw, nh)
        rx, ry = nw / w, nh / h
        for track_id, box in items:
            scaled = [box[0] * sx * rx, box[1] * sy * ry, box[2] * sx * rx, box[3] * sy * ry]
            draw_rect(small, nw, nh, scaled, (255, 220, 0))
            label = track_id.split("-")[-1].lstrip("0") or "0"
            draw_text(small, nw, nh, int(scaled[0]), max(0, int(scaled[1]) - 28), label, scale=3)
        panel = tmp / f"panel_{chunk_index}_{frame_index}.jpg"
        to_jpeg(small, nw, nh, panel)
        w, h, buf = nw, nh, small
        panels.append(panel)
        notes.append(
            {
                "chunk_index": chunk_index,
                "filename": chunk["filename"],
                "segment_number": segment_number,
                "frame_index": frame_index,
                "time_s": time_s,
                "time_in_segment_s": frame["time_sec"],
                "decoded_wh": [w, h],
                "det_shape": shape,
                "fps": raw.get("fps"),
                "track_ids": [tid for tid, _ in items],
            }
        )
    out = ROOT / "out"
    out.mkdir(parents=True, exist_ok=True)
    dest = out / "debug-3frames.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(panels[0]), "-i", str(panels[1]), "-i", str(panels[2]),
         "-filter_complex", "hstack=inputs=3", str(dest)],
        check=True,
    )
    (out / "debug-3frames.json").write_text(json.dumps(notes, indent=2) + "\n")
    print(json.dumps(notes))


if __name__ == "__main__":
    main()
