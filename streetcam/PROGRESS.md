# streetcam progress: neighborhood_cam-1 tracks

Camera `neighborhood_cam-1`. Chunks came from `GET /api/v1/videos/explore` (612 indexed chunks). This camera is 52 of them. Capture day is the date in the filename (`neighborhood_YYYYMMDD`), not the upload stamp.

| Capture day | Chunks | Segments fetched | Frames in sidecars | Tracks |
|---|---|---|---|---|
| 20260901 | 30 (0000–0029) | 180 | 18000 | 156 |
| 20260902 | 22 (0000–0021) | 127 | 12655 | 372 |

`streetcam/fetch_detections.py` saved 307 segment files from `GET /api/v1/videos/detections`. Failed: 0. The last chunk of 20260902 (`..._chunk_0021.mp4`) is 2.773 s and has 1 segment.

The sidecar for `20260901` chunk 0000 segment 1 says `fps` 20.0, `frame_count` 100, and frame `shape` `[1154, 2048]` (height, width). A downloaded segment of that day probes as 2048×1154, container rate 498/25. Boxes were not recomputed.

`streetcam/tracker.py` links boxes with greedy IoU (threshold 0.30) against a constant-velocity prediction, and keeps a track open for 2.0 s so it can cross segments and chunks. `still_seconds` sums time where the center moves slower than 20 px/s and the gap is at most 0.5 s. Each day was run on its own output folder.

Both `summary.json` files list every chunk. Chunks with zero tracks: none.

Boundary check (best IoU between the last boxes of chunk n and the first boxes of chunk n+1): on 20260901 the 29 boundaries run from 0.9517 to 1.0. On 20260902, 20 of 21 are high and one is 0: chunk 0007 to chunk 0008, because chunk 0008 first frame has no boxes (`n_start` 0).

Combined tracks: `streetcam/out/tracks.jsonl` (528 lines).

Debug JPEG: `streetcam/out/debug-3frames.jpg`, three frames of 20260901 with ids drawn.

| Frame | File | Segment | Frame | Ids |
|---|---|---|---|---|
| left | chunk 0000 | 3 | 80 | 0001, 0002, 0003, 0004 |
| middle | chunk 0015 | 1 | 0 | 0001, 0002 |
| right | chunk 0029 | 3 | 66 | 0002, 0155 |

From `day-20260901/tracks.jsonl`: track 0002 is a car seen on all 18000 frames (0.0 s to 899.95 s), `still_seconds` 841.9. Track 0001 is a car from 0.0 s to 634.3 s (12687 boxes), `still_seconds` 608.5.

Day folders were produced separately: 20260901 in `streetcam/day-20260901/`, 20260902 in `streetcam/day-20260902/`.
