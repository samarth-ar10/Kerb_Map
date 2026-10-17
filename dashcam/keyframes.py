"""Choose keyframes and, for selected sets, save a small JPEG of each one.

Without data/pie_map.csv, one frame per second. When that file exists and has
a speed_mps column, one frame per about 8 m of travel (step = 8 / speed_mps).
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path

from session import login, stream_url

ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "data" / "pie_map.csv"
SPEED_COLUMN = "speed_mps"


def _speed_table(path: Path) -> dict[tuple[str, int], float] | None:
    if not path.is_file():
        return None
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        if SPEED_COLUMN not in fields:
            raise SystemExit(
                f"{path} exists but has no {SPEED_COLUMN} column (found {fields})"
            )
        speeds: dict[tuple[str, int], float] = {}
        for row in reader:
            speeds[(row["set"], int(row["chunk"]))] = float(row[SPEED_COLUMN])
    return speeds


def keyframe_times(
    duration_sec: float,
    set_id: str,
    chunk: int,
    map_path: Path = MAP_PATH,
) -> list[float]:
    speeds = _speed_table(map_path)
    step = 1.0
    if speeds is not None:
        speed = speeds.get((set_id, chunk))
        if speed is None or speed <= 0:
            raise SystemExit(f"no positive {SPEED_COLUMN} for {set_id} chunk {chunk}")
        step = 8.0 / speed
    times: list[float] = []
    cursor = 0.0
    while cursor < duration_sec - 1e-6:
        times.append(round(cursor, 3))
        cursor += step
    return times or [0.0]


def save_jpeg(backend: str, token: str, source: str, time_sec: float, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = stream_url(backend, token, source)
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{time_sec:.3f}",
        "-i",
        url,
        "-frames:v",
        "1",
        "-vf",
        "scale=160:-2",
        "-q:v",
        "8",
        str(dest),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if completed.returncode != 0 or not dest.is_file():
        tail = (completed.stderr or "")[-240:].replace("\n", " ")
        raise RuntimeError(f"ffmpeg failed for t={time_sec}: {tail}")


def export_frames(segments_csv: Path, frames_dir: Path, map_path: Path) -> dict:
    backend, token = login()
    written = 0
    failed = 0
    with segments_csv.open(newline="") as handle:
        for row in csv.DictReader(handle):
            duration = float(row["end_sec_in_set"]) - float(row["start_sec_in_set"])
            times = keyframe_times(duration, row["set"], int(row["chunk"]), map_path)
            for time_sec in times:
                name = (
                    f"{row['set']}_chunk{int(row['chunk']):04d}"
                    f"_seg{int(row['segment_number']):02d}_t{time_sec:.1f}.jpg"
                )
                dest = frames_dir / name
                if dest.is_file() and dest.stat().st_size > 0:
                    written += 1
                    continue
                try:
                    save_jpeg(backend, token, row["segment_source_uri"], time_sec, dest)
                    written += 1
                except Exception:
                    failed += 1
    return {"written": written, "failed": failed, "dir": str(frames_dir)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Save small keyframe JPEGs")
    parser.add_argument("--segments", type=Path, required=True)
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--pie-map", type=Path, default=MAP_PATH)
    args = parser.parse_args()
    summary = export_frames(args.segments, args.frames, args.pie_map)
    print(summary, file=sys.stderr)


if __name__ == "__main__":
    main()
