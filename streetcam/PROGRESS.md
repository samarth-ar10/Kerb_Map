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


## Kerb drawing and occupancy

Marked on `20260901` chunk 0000, segment 3, frame 80 (`streetcam/frame_neighborhood.jpg`, 2048×1154). The drawing is `streetcam/kerb_neighborhood.json`. `streetcam/kerb_draw.html` paints it on that frame.

Measured on that frame:

- Track 0001 box width is 285 px (x 460–745), side view. Used as 4.8 m, so 59.375 px/m along the near kerb.
- At x=880 the carriageway runs from the far-curb edge y=640 to the near-curb edge y=828, 188 px. Taken as 8 m curb to curb. That 23.5 px/m is the foreshortened across-road scale, not the along-kerb scale.
- Track 0022's mean box is 95 px tall against the SUV box height of 208 px. Far-kerb scale is 59.375 × 95/208 = 27.13 px/m.

The near legal stretch, between the two driveways, is 5.157 m. The far curb is 21.907 m. A free space counts only at 5.5 m or more, so an empty near stretch holds 0 spaces and an empty far stretch holds 3.

Still means the box centre stays inside a 25 px square. Track 0002, which does not move, has a centre range of about 10 by 8 px over the whole day; a 20 px/s frame-to-frame test was splitting that into runs shorter than 120 s.

`streetcam/parked.py` on both day track files:

| Day | Tracks | not_space | traffic | parked | Timeline rows |
|---|---|---|---|---|---|
| 20260901 | 156 | 97 | 59 | 0 | 180 |
| 20260902 | 372 | 275 | 97 | 0 | 128 |

No track is parked. The two cars that stay still on 20260901 are in the driveways: 0001 for 631.5 s (left) and 0002 for 900.0 s (right). Track 0022 stays in a 25 px patch for 135.4 s, and its bottom centre is in the lane, so it is traffic. Occupied intervals are empty on both days. `streetcam/out/occupancy_timeline.jsonl` has both stretches at every 10 s, with the free length equal to the whole stretch.
