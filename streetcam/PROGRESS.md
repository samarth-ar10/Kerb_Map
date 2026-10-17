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


## Occupancy fix (2026-10-17): redrawn areas, position-based occupancy

Why: 40 human labels (batches A3 + A6, `day-of/label-batches/merged_labels.csv`) agreed with the old states on 25. Eight cars parked at the kerb were called traffic (seven "in the lane": the far strip started at x=700, the parked car sits at x 440-760), four moving cars on the bend at the right were outside every polygon, and night detections flicker, so one parked car splits into many short tracks that never pass a per-track 120 s still test.

### Areas (`kerb_neighborhood.json`)

Redrawn from the image geometry (curb lines read off a gridded zoom of `frame_neighborhood.jpg`), not fitted to the labels.

- `far`: 12 px behind the far curb line to 62-65 px in front of it (about 3 m of carriageway), from the tree edge x=430 to the side-road corner x=1085.
- `near`: 45 px into the road to 8 px past the near curb line, x=430 to the end of the grass verge x=1300.
- `lane`: the rest of the carriageway, the side-road mouth, the bend to the right behind the fence, and the road strip left of the tree.
- Driveways `left` and `right` unchanged; added `camera_house` (bottom of the frame, where a car stands at night).
- Each box bottom centre gets exactly one area, in the order driveway, kerb strip, lane, else `off_street`. Checked on all vehicle boxes of both days: 0 boxes fall in more than one polygon. Counts by area: driveway 47024, kerb 18511, lane 4448, off_street 332 (footway, verge, frame edges).
- Along-kerb scale varies along each centreline (`px_per_m_vertices`): median car box width in each 100 px column of the road divided by an assumed 4.5 m car length. The near legal stretch is now 6.172 m (was 5.157), the far legal stretch 17.165 m (was 21.907, it now stops at the side-road corner).
- Overlay check: `out/polygons_check.jpg` (3 day, 3 night frames; every vehicle box bottom at that frame drawn as a dot in its area colour). Regenerate with `python streetcam/polygons_check.py --labels-dir <day-of/label-batches>`. The label-batch pictures still show the old translucent bands under the new outlines.

### Occupancy (`parked.py`)

- Each kerb strip is cut into ~1 m cells (near 18 cells of 0.985 m, far 17 cells of 1.01 m).
- In each frame a cell is covered when the bottom edge of a vehicle box whose bottom centre is in that strip spans it. Coverage is counted per second at 20 fps, independent of track id.
- A cell is occupied in any 120 s window where it is covered in at least 60% of frames; the occupied time is trimmed to the first and last covered second inside that window. Adjacent occupied cells merge into occupied intervals; a connected run in time and along the kerb is one parked period (`out/parked_periods.jsonl`), counted when it lasts at least 120 s.
- Tracks are stitched when one ends and another starts up to 3 s later (or up to 0.5 s before, a duplicate id) within 30 px. The 30 px is measured at the closer bottom corner, not the centre: at night the same parked car's box right edge swings between x=571 and x=770 with glare while the left edge and bottom stay put.
- A track is parked when its stitched group spends at least 120 s on occupied cells, or when at least half of its own boxes sit on a parked period. The second rule was added after the first run on the dev labels found the night car still split into groups of 1-11 tracks, none reaching 120 s. Otherwise the track's majority area decides: kerb strip or lane is traffic, driveway or off_street is not_space.
- Output formats unchanged; extra keys in `track_states.jsonl` (`area`, `group_id`, `group_n_tracks`, `occupied_overlap_s`, `share_on_parked_period`, `why`). `still_run_s` now holds the group's time on occupied cells.

| Day | Tracks | not_space | traffic | parked | Stitched groups | Parked periods | Timeline rows |
|---|---|---|---|---|---|---|---|
| 20260901 | 156 | 12 | 142 | 2 | 141 | 1 (far kerb, 58-238 s, 0-2.0 m) | 180 |
| 20260902 | 372 | 253 | 50 | 69 | 252 | 1 (far kerb, 0-633 s, 0-6.1 m) | 128 |

Both parked periods are on the far kerb at the left, next to the tree. On 20260901 the car (track 0022) is seen end-on and partly hidden by the tree, so the occupied length (2.0 m) is shorter than the car. The 69 parked tracks on 20260902 are fragments of the one car parked there all night. No near-kerb cell was occupied for a qualifying window on either day.

### Agreement with the labels (`out/eval_labels.json`)

These 40 labels (A3, A6) were looked at while making this fix, so this is a development score, not a held-out one.

- Before (old states): 25 of 40. After: 34 of 40. Fixed 10, broken 1.
- Confusion after (rows system, columns human; parked / traffic / not_space): parked 6 / 0 / 1, traffic 2 / 6 / 2, not_space 0 / 1 / 22.
- Still wrong: 20260901-0020 (driveway car, called traffic), 20260901-0093 (truck the labeller called parked, 1.7 s track, called traffic), 20260902-0117 (labelled traffic, called not_space in driveway right), 20260902-0119 (labelled driveway, now called parked: a short track on the parked car's cells), 20260902-0316 (labelled driveway, now traffic: majority of boxes in the lane), 20260902-0334 (labelled parked, 1 s track, majority in the lane).

Held-out check: batches A1, A2, A4, A5 when returned. Run once after the fix was final, on what had come back (A1, A2, A4; A5 not yet). Items that also sit in A3/A6 (overlap slots) are dropped. A2 alone: 18 items, 17 agree before and 17 after (1 wrong both times, 20260901-0154 labelled driveway, called traffic). A1 + A2 + A4: 51 items scored (1 cant_tell left out), 41 agree before, 45 after, 4 fixed, 0 broken (`out/eval_heldout.json`). `python streetcam/eval_heldout.py --labels-dir <day-of/label-batches>` scores every CSV in `returned/` except A3/A6 and writes `out/eval_heldout.json`. `--dev` rewrites `out/eval_labels.json` from `merged_labels.csv`.

Re-run: `python streetcam/parked.py --kerb streetcam/kerb_neighborhood.json --tracks streetcam/day-20260901/tracks.jsonl streetcam/day-20260902/tracks.jsonl --out streetcam/out/occupancy_timeline.jsonl`

Not done: no kerb strip on the outer curb of the bend at the right (traffic only there in these two days); per-vertex scale rests on an assumed car length.
