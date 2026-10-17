# Kerb Map: on-street parking from dashcams, then street cameras

A person drops a pin or enters a postcode. The page shows each stretch of kerb nearby: is it a legal space, is it free or
occupied, when was it last seen, and the evidence for that answer. Every answer is a **receipt**: the frames, the decision
trail, and a confidence. "Cannot tell" and "not seen" are honest answers, never hidden.

**Order of work today:** first the dashcam pipeline on the Toronto PIE drives (phase 1). Then street cameras (phase 2).
Then the postcode/pin query that merges both (phase 3).

## 0. The service

**Your dashcam backs itself up to the cloud as you drive, and in return you get a live map of free kerb space where
you're going.**

1. **Sync, near-live:** the dashcam or phone uploads each 30 s clip to VAST storage as it is recorded (within about a
   minute, not daily or weekly). Users keep a searchable log of their own drives.
2. **Process in the cloud:** each new clip triggers the pipeline (stored boxes, kerb geometry, Cosmos only on unclear
   gaps); receipts land in VastDB. Nothing heavy runs on the device.
3. **Use:** find kerb space around you or around a destination; every answer shows when it was last seen.
4. **Freshness grows with users:** more synced cars = fresher map; fixed street cameras keep busy kerbs live.

**Demo:** a "live replay" mode feeds one supplied Toronto drive into the app clip by clip, in real time, and the map
fills in as each clip arrives. It is labelled on screen as replayed from the supplied footage (organiser rule: no
outside video).

## 1. Approach and targets

The Toronto PIE dashcam drives in the library (public dataset, 6 drives) are the first source, with YOLO boxes, kerb
geometry, OpenStreetMap rules and NVIDIA Cosmos. Labels are written and frozen before any model answer, and a held-out
set is run once.

| Measure | Target |
|---|---|
| Is this a real legal kerb space? (precision on answered spots) | 0.90 |
| Free vs occupied, on real spaces answered | 0.85 |
| When it says "free", is it right? | 0.80 |
| Spaces found / free spaces found (recall) | report it |
| "Cannot tell" rate | under 25% |
| Pin error (median) | under 8 m |
| Share of kerb slots sent to Cosmos | under 5% |

Design choices:
- **Occupied is easier than free.** A single dashcam pass sees parked cars well; an empty gap is easy to miss or to
  confuse with a driveway. That is why street cameras matter (phase 2): a fixed view watches the same kerb for minutes.
- **Watch for false spaces:** driveways, kerbs nobody parks on, corners, hydrants.
- **Cheap first, Cosmos last:** distance gate, then boxes, then geometry and rules, then Cosmos only on unclear gaps.

## 2. Facts about the event data we must design around

- VSS rows store `location` as one word (e.g. `toronto`), no GPS, no road name, no capture clock. Boxes are stored per
  5 s segment (`detection_sidecar_uri`, YOLO11s, conf >= 0.4, no track ids). Check this first in the clip table.
- A **postcode or pin needs coordinates.** Dashcam clips only get them if GPS comes with the footage (PIE has it in its own
  files; ask the organisers if we may use it). A **street camera has one fixed location**, which we can set by hand once.
- Re-ingest is slow. Start any re-ingest in the first hour, on a small batch. Never make the demo wait on it.
- Check how the event's Cosmos model (`cosmos3` family) answers: thinking text, log probabilities, image input. Test the
  prompt and the confidence line on a few labelled spots before trusting it.

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

## 4. Phase 1: the dashcam pipeline on the event stack

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

## 7. Pitfalls to avoid

- Distance from a single frame (flat-road assumption or a monocular depth model) is too noisy to separate kerb,
  driveway and lane. Use the road geometry from OpenStreetMap and GPS.
- Matching cars between keyframes far apart without a tracker makes parked cars look like they move.
- Asking Cosmos "parked or traffic?" about a whole frame is too vague. Ask about one boxed gap.
- Search on generic captions won't find "free kerb space". Filter on the receipt state first, then rank.
- On a **dashcam**, "the car stood still" doesn't mean parked: cars queued at lights stand still too. Useful extra cues:
  road segmentation (spots cars on driveways) and gaps measured across frames.
- On **street cameras**, a car at a red light stands still for 30 to 90 s. Parked means still for longer than a light
  cycle (about 2 minutes) **and** on the kerb strip, not in a lane. A fixed camera can measure that duration.

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

**Cursor agents** do most of the typing. Each builder runs their own agents on the lab VM, and every task is a short prompt
with a clear "done when" from the tables above.
- **What they do:** write and run the code for their workstream; use the starter-kit skills (login, explore, detections,
  re-ingest, VastDB read/write, deploy); split bigger tasks across sub-agents (e.g. one per PIE set or per camera);
  write tests and a short note of what they ran and what came out.
- **What they don't do:** invent numbers, labels or results; change frozen labels; re-ingest hours of video without the
  team agreeing; print or copy credentials; push to `main` without a person reviewing.
- **How we work with them:** one branch per workstream (`dashcam/…`, `streetcam/…`, `query/…`, `design/…`), small commits,
  merge after a person checks the output against the "done when".

## 10. Privacy

Point the processing at kerbs. Occupancy needs no faces or number plates: don't collect them, and don't show plates in
receipt crops. Store the intervals and receipts; keep raw video only as long as the library already does.

## 11. Credits

PIE dataset (York University, MIT licence), Ultralytics YOLO11 (AGPL-3.0), OpenStreetMap (ODbL), NVIDIA Cosmos models,
VAST VSS blueprint and the builders-challenge starter kit. Ideas from Ultralytics ParkingManagement and ORB-SLAM keyframing.
