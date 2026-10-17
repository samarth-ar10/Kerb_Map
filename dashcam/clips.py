"""List Toronto PIE segments. One row per 5 s segment. No baked-in clip lists."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

from session import get_json, login

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dashcam" / "out"
INDEXED_CHUNK_SEC = 30.0
SET_RE = re.compile(r"(set\d+)_video_chunk_(\d+)")
FIELDS = [
    "set",
    "chunk",
    "segment_number",
    "segment_source_uri",
    "detection_sidecar_uri",
    "start_sec_in_set",
    "end_sec_in_set",
]


def explore_toronto(backend: str, token: str) -> list[dict]:
    chunks: list[dict] = []
    offset = 0
    total = None
    while total is None or offset < total:
        page = get_json(
            backend,
            token,
            f"/api/v1/videos/explore?scope=all&limit=100&offset={offset}&location=toronto",
        )
        batch = page.get("chunks") or []
        total = page.get("total") or 0
        chunks.extend(batch)
        if not batch:
            break
        offset += len(batch)
    return [c for c in chunks if c.get("camera_id") == "pie_cam-3"]


def parse_set_chunk(filename: str, chunk_index: int | None) -> tuple[str, int]:
    match = SET_RE.search(filename or "")
    if not match:
        raise RuntimeError(f"filename is not a PIE chunk: {filename}")
    set_id = match.group(1)
    from_name = int(match.group(2))
    if chunk_index is not None and int(chunk_index) != from_name:
        raise RuntimeError(f"{filename} chunk_index {chunk_index} disagrees with the name")
    return set_id, from_name


def rows_for_chunk(backend: str, token: str, chunk: dict) -> list[dict]:
    filename = chunk.get("filename") or ""
    set_id, chunk_no = parse_set_chunk(filename, chunk.get("chunk_index"))
    original = chunk["original_video"]
    payload = get_json(
        backend,
        token,
        "/api/v1/tools/segments?original_video=" + quote(original, safe=""),
    )
    rows = []
    for segment in payload.get("segments") or []:
        start = float(segment["segment_start_sec"])
        end = float(segment["segment_end_sec"])
        base = chunk_no * INDEXED_CHUNK_SEC
        rows.append(
            {
                "set": set_id,
                "chunk": chunk_no,
                "segment_number": int(segment["segment_number"]),
                "segment_source_uri": segment["source"],
                "detection_sidecar_uri": segment.get("detection_sidecar_uri") or "",
                "start_sec_in_set": round(base + start, 3),
                "end_sec_in_set": round(base + end, 3),
            }
        )
    rows.sort(key=lambda row: row["segment_number"])
    return rows


def write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def fetch_rows(set_id: str | None) -> list[dict]:
    backend, token = login()
    chunks = explore_toronto(backend, token)
    selected = []
    for chunk in chunks:
        parsed, _ = parse_set_chunk(chunk.get("filename") or "", chunk.get("chunk_index"))
        if set_id is None or parsed == set_id:
            selected.append(chunk)
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(rows_for_chunk, backend, token, chunk) for chunk in selected]
        for future in as_completed(futures):
            rows.extend(future.result())
    rows.sort(key=lambda row: (row["set"], row["chunk"], row["segment_number"]))
    return rows


def merge_csvs(paths: list[Path], dest: Path) -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple] = set()
    for path in paths:
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                row["chunk"] = int(row["chunk"])
                row["segment_number"] = int(row["segment_number"])
                row["start_sec_in_set"] = float(row["start_sec_in_set"])
                row["end_sec_in_set"] = float(row["end_sec_in_set"])
                key = (row["set"], row["chunk"], row["segment_number"])
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
    rows.sort(key=lambda row: (row["set"], row["chunk"], row["segment_number"]))
    write_rows(dest, rows)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the PIE segment table")
    parser.add_argument("--set", dest="set_id", help="Only this set, e.g. set01")
    parser.add_argument("--out", type=Path, help="CSV path")
    parser.add_argument("--merge", nargs="*", type=Path, help="Combine per-set CSVs into clips.csv")
    args = parser.parse_args()
    if args.merge:
        dest = args.out or (OUT / "clips.csv")
        rows = merge_csvs(args.merge, dest)
    else:
        dest = args.out or (OUT / "clips.csv")
        rows = fetch_rows(args.set_id)
        write_rows(dest, rows)
    print(f"rows {len(rows)} -> {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
