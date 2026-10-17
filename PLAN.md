# Kerb Map: on-street parking from dashcams, then street cameras

A person drops a pin or enters a postcode. The page shows each stretch of kerb nearby: is it a legal space, is it free or
occupied, when was it last seen, and the evidence for that answer. Every answer is a **receipt**: the frames, the decision
trail, and a confidence. "Cannot tell" and "not seen" are honest answers, never hidden.

**Order of work today:** first rebuild, on the VAST event stack, the dashcam pipeline we designed and measured before the
event (phase 1). Then add street cameras (phase 2). Then the postcode/pin query that merges both (phase 3).
All code in this repo is written today. What we bring from before the event is the plan, the measurements, and the
lessons below.

## 1. What we already know (measured before the event)

We rehearsed this on the public Toronto PIE dashcam dataset (85 minutes, 6 drives) with YOLO11, kerb geometry,
OpenStreetMap rules and NVIDIA Cosmos-Reason1-7B running locally. Labels were frozen before any model answer. A fresh
held-out set (9 videos never used for tuning) was run once.

| Measure (fresh held-out) | Result | Target |
|---|---|---|
| Is this a real legal kerb space? (precision on answered spots) | 0.84 (38/45) | 0.90 |
| Free vs occupied, on real spaces answered | 0.895 (34/38) | 0.85 |
| When it says "free", is it right? | 0.53 (8/15) | 0.80 |
| Spaces found / free spaces found (recall) | 0.42 / 0.17 | - |
| "Cannot tell" rate | 4.5% | under 25% |
| Pin error (median) | about 3 m | under 8 m |
| Cosmos calls | 2.3% of kerb slots, 76 GPU-s per camera-hour vs 2,600 for captioning everything | - |

What this tells us:
- **Occupied is easy, free is hard.** A single dashcam pass finds parked cars well but misses or mislabels free gaps.
  That is why street cameras matter (phase 2): a fixed view watches the same kerb for minutes.
- **The false spaces** were driveways (3 of 7), kerbs nobody parks on, a corner, a hydrant.
- **Cheap first, Cosmos last** works: distance gate, then boxes, then geometry and rules, then Cosmos only on unclear gaps.

## 2. Facts about the event data we must design around

- VSS rows store `location` as one word (e.g. `toronto`), no GPS, no road name, no capture clock. Boxes are stored per
  5 s segment (`detection_sidecar_uri`, YOLO11s, conf >= 0.4, no track ids). Check this first in the clip table.
- A **postcode or pin needs coordinates.** Dashcam clips only get them if GPS comes with the footage (PIE has it in its own
  files; ask the organisers if we may use it). A **street camera has one fixed location**, which we can set by hand once.
- Re-ingest is slow. Start any re-ingest in the first hour, on a small batch. Never make the demo wait on it.
- Cosmos on the event host is a different model from our rehearsal (`cosmos3` family). Re-check the prompt and the
  confidence line on a few labelled spots before trusting it.

## 3. The data model (one model for every source)

Each road side is a kerb line. Along it there are intervals:
- **Legal:** where a car may stop (no junctions, bus stops, hydrants, crossings, driveways, double yellows).
- **Seen:** metres a camera actually had in view, with the time.
- **Occupied / free / cannot tell:** what was there.

A free space is a legal, seen, unoccupied gap of about one car length (5.5 m). Unseen kerb stays **unknown**, which is
different from **empty**.

Stored in VastDB beside `vss-collection` (same schema):

| Table | One row per |
|---|---|
| `kerb_spaces` | kerb stretch: state, side, position or "position uncertain", last seen, confidence |
| `kerb_observations` | sighting: source (dashcam or street camera), video, segment, time, state |
| `kerb_trail` | decision step: gate, boxes, geometry, rule, Cosmos word and confidence |
| `kerb_overrules` | person or agent correction, and which receipts it changed |
| `kerb_eval_cases` | frozen label, used for measurement |

## 4. Phase 1: rebuild the dashcam pipeline on the event stack

| # | Step | Done when |
|---|---|---|
| 1 | Clip table: every clip, dashcam or street camera, location word, GPS yes/no, duration | table in `data/clips.csv` |
| 2 | Keyframes: one per ~8 m of travel (distance gate) or one per second without GPS | frames + index written |
| 3 | Boxes: read the stored YOLO sidecar for the keyframe time (don't re-run YOLO) | boxes for every keyframe |
| 4 | Kerb slots: parked-car rows along each side, and the gaps between them | slots with left/right side |
| 5 | Rules: OSM junctions, crossings, hydrants, bus stops; car-length minimum | rejected slots carry a reason |
| 6 | Cosmos: only on unclear gaps; answer free / occupied / not a space / cannot tell + confidence | under 5% of slots sent |
| 7 | Position: GPS + offset, snapped to the road; else "position uncertain" | pin or uncertain on every receipt |
| 8 | Receipts into the five VastDB tables | rows visible in VastDB |
| 9 | Query: one VastDB filter (state, side) + radius + semantic similarity over the evidence | returns receipts in under 3 s |
| 10 | Overrule: a person or the agent corrects a receipt; nearby receipts re-run; change visible | before/after shown |
| 11 | MCP tools `find_free_space`, `get_receipt`, `overrule`, used by a second agent | 5 tasks, incl. an abstention |
| 12 | Measure: frozen labels (human-checked), dev vs held-out, counts beside every number | results table in README |

## 5. Phase 2: street cameras (fixed CCTV view of a kerb)

A fixed camera can watch one stretch continuously, which fixes our weakest number (free spaces).

1. Pick clips where a fixed camera sees a kerb (the neighbourhood and street cameras in the library).
2. Draw the kerb once by hand on one frame: the kerb line, legal stretches, and 3 to 4 reference points to map pixels to
   metres along the kerb.
3. **Track** cars over time (YOLO11 + tracker for persistent ids). A car that stays still for more than a few seconds,
   on the kerb strip, is parked. Moving cars are traffic.
4. Project each parked car's footprint onto the kerb line as an occupied interval. The visible stretch is "seen".
5. Every few seconds write the intervals. Keep receipts with a frame, not the whole video.
6. Measure it the same way: label 30 to 50 moments by hand before running, report free precision and recall.

One camera covers its near kerb well and loses the far end. Report the covered metres, never more.

## 6. Phase 3: pin / postcode query and merging sources

- Load kerb lines near the pin, apply rules, keep fresh sightings, count car-length gaps on seen, legal kerb.
- Newer sighting of the same metres wins. Street cameras refresh their stretch; dashcams fill other roads, then go stale.
- Answer per road side: free count, metres seen, last seen, and a link to each receipt.

## 7. What we tried before the event that did NOT work (don't repeat)

- Distance from a single frame using a flat-road assumption, or a monocular depth model: too noisy to separate kerb,
  driveway and lane.
- Matching cars between keyframes 1 to 2 s apart without a tracker: parked cars looked like they moved.
- Asking Cosmos "parked or traffic?" on the whole frame: cut recall badly. Ask about one boxed gap instead.
- A zoomed kerb crop as a second look, "could you park here?", a three-frame clip, OSM driveway distance: no gain.
- Captions alone for search: "free kerb space" search fails on captions. Filter on the receipt state first.

What is promising and still open: tracking cars through the video plus road segmentation (road vs sidewalk), which
street cameras give us almost for free.

## 8. How we measure (judges ask for this)

- Labels are written and frozen (hash + time) before the model runs on them, checked by a person.
- Dev and held-out are separate. Held-out is run once and reported as it comes.
- Every number has its count beside it. "Cannot tell" is reported, not hidden.
- One decision trail is shown end to end in the demo.
- One VastDB query combines a filter with semantic search.

## 9. Who does what

Four people plus Cursor agents. One person designs; three build, each owning one workstream and driving their own
Cursor agents (ask them to use sub-agents). Workstreams meet at the five VastDB tables in section 3, so they can run in
parallel.

| Who | Workstream | Owns | Done when |
|---|---|---|---|
| Builder A | Dashcam pipeline (phase 1, steps 1–8) | clip table, keyframes, stored boxes, kerb slots, rules, Cosmos check, position, receipts written to VastDB | PIE receipts in `kerb_spaces` with trails |
| Builder B | Street cameras (phase 2) | car tracking from the stored per-frame boxes, kerb drawn on the neighbourhood camera, "stays still = parked", occupied intervals | neighbourhood camera writes intervals to the same tables |
| Builder C | Query, agent and measurement (phase 1 steps 9–12, phase 3) | VastDB filter + semantic query, overrule loop, MCP tools and second agent, frozen labels and the results table | pin query returns receipts; results table in README |
| Designer | Product and demo | map and receipt screens, the 3-minute demo flow, slides, demo video by 16:00 | demo clicks through end to end |

Samarth also coordinates the plan, reviews merges, runs the human label check and checks every number before it goes on a slide
(with an AI research assistant).

## 10. Privacy

Point the processing at kerbs. Occupancy needs no faces or number plates: don't collect them, and don't show plates in
receipt crops. Store the intervals and receipts; keep raw video only as long as the library already does.

## 11. Credits

PIE dataset (York University, MIT licence), Ultralytics YOLO11 (AGPL-3.0), OpenStreetMap (ODbL), NVIDIA Cosmos models,
VAST VSS blueprint and the builders-challenge starter kit. Ideas from Ultralytics ParkingManagement and ORB-SLAM keyframing.
