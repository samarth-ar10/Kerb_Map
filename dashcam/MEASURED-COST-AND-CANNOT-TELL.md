# Measured: cost per camera-hour, and why "cannot tell"

Team dashcam pipeline (`dc_`), Toronto PIE drives set01-set06, `pie_cam-3`: 1,080 segments x 5 s = **1.5 camera-hours**.
All numbers are counted from stored files or from runs on 2026-10-17. Same numbers in `measured.json`.
Code: branch `dashcam/cannot-tell` (`dashcam/receipts.py` flag, `dashcam/cannot_tell.py`).

## 1. Cost and latency

| Per camera-hour | Value | Total (1.5 h) | Source |
|---|---|---|---|
| Keyframes kept | 1,800.7 | 2,701 of 162,000 frames (1.67%) | `out/work/<set>_stats.json` |
| YOLO boxes stored (all frames) | 811,469 | 1,217,203 | detections counted in the 1,080 stored files (`bystander-data/dashcam-det`), not recomputed |
| YOLO boxes read at keyframes (all classes) | 11,139 | 16,709 | same files, keyframe frames only |
| Vehicle boxes used (car/truck/bus >= 0.4) | 7,521 | 11,281 | same |
| Slots | 5,231 | 7,846 | stats |
| Cosmos calls | 2.67 | 4 | 4 `cosmos` rows in `kerb_trail` (dc_) |
| Cosmos seconds (uncached) | 13.0 | 19.5 (4.81, 4.99, 4.88, 4.82 s) | first-run call means, `dashcam/RESULTS.md`, local Cosmos-Reason1-7B Q4 on the team laptop |
| Wall time per 30 s clip | 1.64 s | 180 clips | 235 s fetch + 5.2 s keyframes-to-position + 19.5 s Cosmos + 36 s write, from `RESULTS.md`, / 180 |

Per clip: fetch 1.31 s, keyframes/boxes/geometry/rules/position 0.03 s, Cosmos 0.11 s (average), write 0.20 s.
Cached Cosmos calls in the trail: 0.42, 0.33, 0.35, 0.10 s. Eight more calls today on four stored frames: 0.11-0.38 s
(the server cached every image, so no new uncached number was obtained).

| Baseline: Cosmos on every 5 s segment | Value |
|---|---|
| Calls per camera-hour | 720 |
| Seconds per call | 4.875 s (mean of the 4 uncached means above; team laptop) |
| Cosmos seconds per camera-hour | 3,510 s (58.5 min) |
| Ours vs baseline | 270x fewer calls, 270x fewer Cosmos seconds (13.0 s vs 3,510 s) |

The event endpoint (`nvidia/cosmos3-nano-reasoner`, `out/cosmos_probe.json`) has no timing stored, so its per-call
time is **not measured**.

**Clip arrival to receipts, one 30 s clip** (set05 chunk 0024, run today from stored detections to receipt JSONL):

| Step | Seconds |
|---|---|
| Keyframes + boxes + geometry + rules + position (6 segments, 67 slots) | 0.17 |
| Cosmos stage (0 gaps queued in this clip) | 0.05 |
| Merge sightings + receipt text (34 receipts: 22 occupied, 9 cannot_tell, 2 not_space, 1 free) | 0.02 |
| **Measured total** | **0.23** |
| Not in the run: fetch detections (235 s / 1,080 segments x 6, derived) | 1.31 |
| Not in the run: one uncached Cosmos call, if a gap is queued | 4.875 |
| **Estimate with fetch and one Cosmos call** | **6.41** |

SQLite write was not run (it would replace the lab rows); frame cutting on the VM was not measured. The drive's
calibration was reused. Keyframes restart at the clip start, so this clip's frames differ from the full-drive run,
and its two model-checked gaps did not come up.

## 2. Why "cannot tell"

**By final reason (7,846 slots)**: cannot_tell 5,516, occupied 2,272, not_space 47, free 11.

| Final reason | Slots |
|---|---|
| Lane unclear (car) | 5,482 |
| Lane unclear (gap behind a car of unclear lane) | 30 |
| Kerb hidden by traffic in our lane | 3 |
| Model low confidence | 1 |
| Model unavailable / not sent (budget) / OSM or position missing | 0 |

**What triggered "lane unclear" (5,482 car slots)**

| Trigger | Slots |
|---|---|
| Inner edge closer to our path than the lane margin | 2,925 |
| Both: margin and not in the outermost row | 1,832 |
| Not in the outermost row (margin met) | 725 |

| Lane margin applied | Slots |
|---|---|
| 5.0 m, left side of a two-way road | 3,006 |
| 4.5 m, multi-lane road | 2,069 |
| 1.8 m, single lane our side | 407 |

| Side | Slots | | Short of the margin by | Slots |
|---|---|---|---|---|
| Left | 3,996 | | < 0.5 m | 505 |
| Right | 1,486 | | 0.5-1 m | 536 |
| | | | 1-2 m | 1,171 |
| | | | 2-3 m | 1,347 |
| | | | >= 3 m | 1,200 |
| | | | margin met (row test failed) | 723 |

| Depth (forward, m) | Slots | | Box bottom row (px) | Slots | | Box centre from image centre (px) | Slots |
|---|---|---|---|---|---|---|---|
| < 10 | 1,260 | | < 760 | 11 | | 0-199 | 2,371 |
| 10-15 | 892 | | 760-799 | 1,397 | | 200-399 | 1,616 |
| 15-20 | 952 | | 800-849 | 1,455 | | 400-599 | 719 |
| 20-30 | 1,522 | | 850-899 | 1,351 | | 600-799 | 613 |
| 30-39.9 | 698 | | 900-999 | 1,086 | | >= 800 | 163 |
| 40 (cap) | 158 | | >= 1000 | 182 | | | |

| Segment speed (km/h) | Slots | | Parked row on the same side, same frame | Slots |
|---|---|---|---|---|
| 0-4 | 155 | | yes | 2,356 |
| 5-14 | 732 | | no | 3,126 |
| 15-29 | 2,595 | | | |
| 30-44 | 1,786 | | | |
| >= 45 | 214 | | | |

Reading: most cannot_tell is the left side of two-way roads (3,006), where the 5.0 m margin leaves room for an
oncoming lane; a box there is often genuinely traffic. Depth is spread out, so a long-range band alone would not fix it.

**Example frames** (`day-of\cannot-tell\`, cut from the dashcam label batches; blue box = slot, pink dot = kerb point):

| File | Trigger | Reason |
|---|---|---|
| 1_dc_set02_0011_6_41_L02.jpg | left of two-way road | inner edge 0.94 m, needs >= 5.0 m |
| 2_dc_set03_0000_1_06_L02.jpg | multi-lane road, row test | inner edge 5.38 m meets 4.5 m, not in the outermost row |
| 3_dc_set02_0002_5_10_L02.jpg | single lane | inner edge 0.73 m, needs >= 1.8 m |
| 4_dc_set01_0006_2_12_L06.jpg | gap behind unclear car | a car of unclear lane sits in front of this gap |
| 5_dc_set04_0002_6_08_L05.jpg | kerb hidden | traffic in our lane |
| 6_dc_set02_0005_2_13_L03.jpg | left of two-way road | inner edge 1.13 m, needs >= 5.0 m |

## 3. One rule change: neighbouring parked row

A lane-unclear car whose outer edge is within **1.0 m** of the parked row's outer edge (median of the cars the base
rule already called parked, same side, same frame) is called parked. Only base-rule parked cars define the row (no
chaining). Flag `KERB_ROW_RULE=1` (tolerance `KERB_ROW_TOL_M`), off by default. Re-run on the stored detections, same
code otherwise; the flag-off run reproduces the shipped counts exactly.

| State | Slots before | Slots after | Receipts before | Receipts after |
|---|---|---|---|---|
| cannot_tell | 5,516 | 5,378 | 3,345 | 3,262 |
| occupied | 2,272 | 2,412 | 1,710 | 1,795 |
| not_space | 47 | 61 | 44 | 57 |
| free | 11 | 13 | 11 | 13 |
| **Total** | **7,846** | **7,864** | **5,110** | **5,127** |

Slot changes: 140 cannot_tell -> occupied; 28 new gap slots next to the newly parked cars (17 not_space, 2 free,
9 cannot_tell). One new gap was queued for Cosmos but has no cut frame, so it is cannot_tell (frame unavailable);
Cosmos calls stay at 4. The rule moves 2.5% of cannot_tell slots; it does not touch the two-way-road margin.

**Accuracy: not scored yet.** `returned\` has no `answers_B*.csv`. When they arrive:
`python dashcam/cannot_tell.py score <before work dir> <after work dir>` scores both versions on the labelled dc_
spots (precision/recall for free, occupied, real space = free or occupied, with counts). Split by drive: tune on
set01-03, report on set04-06 only. The 1.0 m tolerance was picked before any labels existed.
