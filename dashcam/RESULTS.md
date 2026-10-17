# Dashcam receipts: counts (Toronto PIE drives set01-set06)

Run 2026-10-17 on the laptop against real supplied data only: stored YOLO11 detections for all 1,080 `pie_cam-3`
segments (fetched read-only from the team VSS, `fetch_detections_vm.py`), GPS from `data/pie_segments.csv`,
OpenStreetMap (one Overpass request per drive, cached), local Cosmos-Reason1-7B (Q4 GGUF) at 127.0.0.1:8850.
Receipts written with `common/kerbdb.py` to the lab SQLite (`KERB_DB=local`) and to `out/<set>_kerb_*.jsonl`.

**No accuracy numbers.** Nothing here has been checked by a person yet. 120 receipts are queued for the team's label
batches in `out/label_candidates.csv`.

## Per drive

| Drive | Segments | Keyframes | Gate share (keyframes / frames) | Slots | Lane unclear (cars / gaps) | Sent to Cosmos | Receipts | free | occupied | not_space | cannot_tell |
|---|---|---|---|---|---|---|---|---|---|---|---|
| set01 | 180 | 532 | 1.97% | 1,859 | 1,435 / 6 | 0 | 1,184 | 1 | 333 | 7 | 843 |
| set02 | 180 | 324 | 1.20% | 814 | 589 / 1 | 1 | 547 | 2 | 159 | 5 | 381 |
| set03 | 180 | 424 | 1.57% | 1,044 | 695 / 6 | 1 | 762 | 2 | 251 | 3 | 506 |
| set04 | 180 | 426 | 1.58% | 1,717 | 1,196 / 5 | 0 | 1,037 | 2 | 351 | 15 | 669 |
| set05 | 180 | 660 | 2.44% | 1,332 | 849 / 7 | 2 | 830 | 2 | 336 | 7 | 485 |
| set06 | 180 | 335 | 1.24% | 1,080 | 718 / 5 | 0 | 750 | 2 | 280 | 7 | 461 |
| **All** | **1,080** | **2,701** | **1.67%** (of 162,000) | **7,846** | **5,482 / 30** | **4 (0.05%)** | **5,110** | **11** | **1,710** | **44** | **3,345** |

- Slot = one kerb sighting in one keyframe (a parked car, a car of unclear lane, or a gap between two parked cars).
  Receipts = slots merged across keyframes (same drive, side and kind, within 3.5 m): 7,846 observations -> 5,110 receipts.
- **Lane unclear** (conservative lane check): a car not clearly in our lane but closer to our path than the kerb row
  should be (>= 1.8 m single-lane, >= 4.5 m multi-lane, >= 5.0 m left of a two-way road) is never called parked or
  traffic: its slot is `cannot_tell`, reason "lane unclear", and no gap is built next to it. That affects
  5,482 car slots and 30 gap slots (70% of all slots); it is the main source of `cannot_tell`.
- Gaps: 92 gap slots (>= 0.6x a neighbouring parked-car width) across all drives: 37 shorter than one car length,
  10 rejected by an OSM rule (junction, driveway, crossing, traffic signals; node id and distance in each receipt),
  33 cannot_tell from geometry (30 lane unclear, 3 kerb hidden by traffic), 8 free by geometry + rules, 4 sent to
  Cosmos (3 free, 1 cannot_tell). After merging sightings: 44 `not_space` receipts (34 short, 10 rule).
- Cosmos: 4 of 7,846 slots (0.05%) sent, all long or weakly bounded gaps. 3 answered FREE with p >= 0.85 and became
  `free`; 1 answered FREE with low confidence and became `cannot_tell`. Uncached calls took 4.8-5.0 s each
  (first runs: 4.81, 4.99, 4.88, 4.82 s means); the final rerun hit the server's prompt cache (0.2-0.4 s, stored in
  `out/work/<set>_stats.json`). In a hand test on a box drawn around a car in traffic the model still answered FREE
  (p 0.82), which is why answers below 0.85 are `cannot_tell` and why model answers need the human labels.

## Time per camera-hour (6 drives = 1.5 camera-hours)

| Stage | Measured | Per camera-hour |
|---|---|---|
| Fetch stored detections (VM, 4 workers, 1,080 segments) | 235 s | 157 s |
| Keyframes + boxes + geometry + rules + position (6 drives) | 5.2 s | 3.5 s |
| Cosmos (4 calls x ~4.9 s uncached) | ~20 s | ~13 s |
| Write to SQLite + JSONL (6 drives) | 36 s | 24 s |

Frame extraction for the Cosmos checks (VM, ffmpeg) is not included (one segment download + one frame per call).

## Assumptions to check (all in `receipts.py` constants)

- Pixels-per-metre from a per-drive fit of rear-view car box width vs bottom row (car width 1.8 m); fitted horizon
  row 732-806 px. Forward distance for the pin assumes a 1000 px focal length (not calibrated), so pins carry an
  along-road error of several metres; side offset comes from the width fit.
- Car length from neighbouring box widths: a gap >= 1.1x the neighbours' mean box width counts as >= one car length.
  Free confidence = 0.8 x the weaker neighbour's box confidence (a heuristic, not a calibrated probability).
- Rule radii: junction 12 m, driveway 6 m, crossing 10 m, signals 15 m, hydrant 5 m, bus/tram stop 15 m, stop 10 m.
- Pins are not snapped to the OSM road line yet.
- `kerb_spaces` has no evidence_start/end columns in the contract: the window is in `receipt_text` and in the
  `gate` trail step (`evidence_start_s`, `evidence_end_s`); `t_sec` holds the keyframe time.

## Teammate receipts converted (`convert_teammate.py`)

Branch `origin/dashcam/pie-receipts` (pipeline "pie-receipts"): 5,302 receipts and 31,812 trail rows read; 5,302
converted, 0 dropped; 5,302 got a GPS position (camera position at the receipt time, `position_status=ok`), 0
uncertain. States after mapping: occupied 4,429, not_space 851 (was "rejected"), cannot_tell 16, free 6; sides L 3,714, R 1,588. See `out/pie_receipts_converted_summary.json`.
Stored with space_id prefix `pr_` (this pipeline uses `dc_`), one kerb_observations row each, trail detail tagged
`"pipeline": "pie-receipts"`.
