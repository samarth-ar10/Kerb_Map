# Dashcam progress

Phase 1, step 1–3 of the clip list, stored boxes, and keyframes. No VastDB writes in this step.

## What ran

- `dashcam/clips.py` listed `location=toronto`, `camera_id=pie_cam-3` from `/api/v1/videos/explore`, then `/api/v1/tools/segments` for each chunk.
- One sub-agent per PIE set wrote `dashcam/out/set0N/segments.csv`. `clips.py --merge` combined them into `dashcam/out/clips.csv`.
- `dashcam/boxes.py` loaded `/api/v1/videos/detections` for every set01 keyframe (car, truck, bus only).
- `dashcam/keyframes.py` saved one JPEG per second for set01 and set02 through `/api/v1/videos/stream` and ffmpeg (`scale=160`). `data/pie_map.csv` is not in the worktree, so the 8 m distance gate was not used.

## What came out

| Check | Result |
|---|---|
| `dashcam/out/clips.csv` | 1,080 segment rows |
| Sets | set01–set06, 180 rows each |
| Chunks | 0–29 in every set |
| Segments | 1–6 in every chunk |
| Missing source or sidecar URI | 0 |
| set01 keyframes | 900 (5 per segment) |
| set01 boxes loaded | 900 yes, 0 no (`dashcam/out/set01/keyframe_boxes.csv`) |
| JPEGs | set01: 900, set02: 900, width 160 px |

Set time is `chunk_index * 30 + segment start/end` inside the chunk. That matches the indexed 6 × 5 s timeline, not a measured drive distance.

## What failed

Nothing in this run. GPS is still absent, so every keyframe is one per second until `data/pie_map.csv` provides `speed_mps`.

## Kerb slots (set01, set02)

`dashcam/slots.py` read the stored car/truck/bus boxes at each keyframe. `dashcam/rules.py` did not call OpenStreetMap: `data/pie_map.csv` is not in the worktree, and `common/kerbdb.py` does not exist. The trail is `dashcam/out/kerb_trail.jsonl` with steps `gate`, `boxes`, `geometry`, and `rule`.

One car length here is the mean width in pixels of the two cars beside a gap. It is not 5.5 m. A shorter gap is rejected. A gap longer than 2.5 of those widths is rejected as a driveway or a kerb with no parking row. A side with no parked-looking row gets no gap.

| Check | set01 | set02 |
|---|---|---|
| Slots | 1,257 | 766 |
| Left / right | 862 / 395 | 446 / 320 |
| Occupied | 975 | 601 |
| Gaps kept | 8 | 4 |
| Rejected | 274 | 161 |
| Rejected with no reason | 0 | 0 |

Rejection reasons across both sets: `size_inconsistent_with_distance` 167, `bus_not_a_parked_car` 149, `shorter_than_one_car_length` 115, `longer_than_two_and_a_half_car_lengths` 4. Every slot has a rule step saying OpenStreetMap was not applied.

## Cosmos receipts (set01–set06)

`dashcam/cosmos_check.py` asked `nvidia/cosmos3-nano-reasoner` at `166.19.38.112:8001` only about gaps the geometry and rules left open. A three-frame probe on set01 JPEGs showed image input works (`image_url` JPEG), `reasoning` and `reasoning_content` are null, and the word is in `message.content` with `logprobs`. The production calls use that image path. Confidence is `exp` of the summed token log probabilities covering the answer word. Below 0.6 the label is `CANNOT_TELL`. Two workers. Every answer is in `dashcam/out/cosmos_cache.jsonl`.

`data/pie_map.csv` is still absent, so every receipt has `position_status=uncertain` and position text `position uncertain`. No side offset was snapped to an OSM road.

| Check | Result |
|---|---|
| Slots | 5,302 |
| Sent to Cosmos | 24 (0.4527% of slots) |
| Cache rows | 24, 0 errors |
| Evidence JPEGs | 24 (`dashcam/out/receipts/`, gap drawn, scaled to 640 px) |
| `kerb_spaces` rows | 5,302, all six sets, 0 missing `receipt_text` or `evidence_uri` |
| `kerb_trail` rows | 31,812 (gate, boxes, geometry, rule, cosmos, position; 0 incomplete) |
| VastDB read-back | `vss-schema.kerb_spaces` 5,302 and `vss-schema.kerb_trail` 31,812, same per-set counts |

Receipts by set: set01 1,257, set02 766, set03 1,057, set04 1,019, set05 372, set06 831.

States: occupied 4,429, rejected 851, free 6, cannot_tell 16. Cosmos labels on the 24 gaps: FREE 6, OCCUPIED 2, CANNOT_TELL 16 (all 16 had confidence under 0.6).

The SDK prints `Listing sorted columns … without elysium enabled` (HTTP 400) while opening these tables. The insert still landed: a later select returned the row counts above.

## Eval sample (not scored)

`dashcam/eval_cases.py prepare` drew 100 spots from identity fields only (seed 20261017). Dev is set01 and set02, 30 each, the two drives the slot step was built on. Held-out is set03, 40 spots, the next drive. That choice was not made from a score. Frames with the spot boxed are in `dashcam/out/eval_frames/` (100 written, 0 errors).

A person still has to label legal/not and free/occupied at `http://127.0.0.1:8765/`. The page does not show receipt states. On the last save it hashes `data/labels/2026-10-17-kerb-spots.csv`, writes `kerb_eval_cases`, and scores once into `dashcam/RESULTS.md`. Those files are not written yet.
