# Kerb Map

On-street parking from dashcams and fixed street cameras. A pin returns each nearby stretch of kerb: whether it is a legal space, whether it is free, occupied, or cannot-tell, when it was last seen, and a receipt with the evidence and the decision trail. Unseen kerb stays unknown. Unknown is a different answer from empty.

The product sketch is [ONE-PAGER.md](ONE-PAGER.md). The build plan is [PLAN.md](PLAN.md). Agent rules are [AGENTS.md](AGENTS.md). Footage is the team library on the [VSS dashboard](https://team-5-vss.thecosmoslabs.com/dashboard).

## Architecture

Both pipelines read boxes that VSS already stored (YOLO11, confidence at least 0.4, no track ids). They do not re-run YOLO. They write the same tables through `common/kerbdb.py`. The app reads those tables, and falls back to the JSONL files the builders committed under `dashcam/out/` and `streetcam/out/`.

![How a receipt is made. The library feeds a dashcam lane and a street-camera lane. Both write the same six tables, and a pin query reads them.](design/how-a-receipt-is-made.png)

![What a receipt holds. A state, a side, a length, a position, when it was last seen, an evidence frame, and the decision trail. Free means legal, seen, and empty for about one car.](design/what-a-receipt-holds.png)

A free space is a legal stretch a camera actually saw, empty for about one car length (5.5 m). A newer sighting of the same metres wins. Street cameras keep one stretch fresh. Dashcams cover other roads, then go stale. `position_status` is `ok` only when the check is under 2 m.

### Dashcam (`pie_cam-3`, Toronto PIE)

`dashcam/receipts.py` is the pipeline. For each drive `set01`–`set06`:

1. **gate** — one keyframe about every 8 m, using speed and distance in `data/pie_segments.csv`.
2. **boxes** — car, truck, and bus boxes from the stored sidecar, in the original 1920×1080 frame.
3. **geometry** — left and right kerb rows, a lane check, and gaps between parked cars.
4. **rule** — OpenStreetMap junctions, driveways, crossings, signals, hydrants, and bus stops.
5. **cosmos** — only the gaps the rules leave open. The local Cosmos-Reason server returns one word and a confidence.
6. **position** — GPS at the keyframe time, plus a forward and side offset. `position_status` is `ok` when that GPS is present.
7. **write** — `kerb_spaces`, `kerb_observations`, and `kerb_trail` through `common/kerbdb.py`, and JSONL under `dashcam/out/`.

Counts and the assumptions behind them are in [dashcam/RESULTS.md](dashcam/RESULTS.md). An earlier run is recorded in [dashcam/PROGRESS.md](dashcam/PROGRESS.md) and converted into the same tables by `dashcam/convert_teammate.py` (space ids prefixed `pr_`). Treat those two write-ups as separate runs.

### Street cameras

Three fixed cameras. The kerb is drawn once, on one frame, in `kerb_neighborhood.json`, `kerb_sf.json`, and `kerb_nyc.json`.

| Camera | What the drawing is | How a window is written |
|---|---|---|
| `neighborhood_cam-1` | Near strip between the driveways, far strip, lane, two driveways. No lat/lon in `data/video_geo.csv`. | One row per stretch per 10 s. Occupied when a parked period covers the stretch. An empty window is `free` with reason `no parked car on the hand-marked legal stretch`. |
| `sf_streets_cam-5` | Broadway & Columbus (Condor Club in view). Landmark is about ±75 m, not the camera. | Same 10 s windows. Legality is unknown, so an empty window is `free` with reason exactly `legality unknown`. |
| `nyc_streets_cam-1` | Broadway & Walker St (401 Broadway in view). Same landmark class. | Same as San Francisco. |

`streetcam/tracker.py` links boxes with greedy IoU against a constant-velocity prediction, and keeps a track open for 2 s so it crosses segments. `streetcam/parked.py` is the occupancy rule. On the neighbourhood camera a kerb strip is cut into cells of about 1 m. A cell is occupied in a 120 s window when a vehicle on that strip covers it in at least 60% of frames. Tracks that flicker are stitched (30 px, 3 s) before that test. On San Francisco and New York the same 120 s test found no parked track: vehicles break into shorter tracks, so those receipts are the empty-window rows above.

`streetcam/geo.py` fits a homography from image pixels on the road to OpenStreetMap nodes, in local east/north metres. `position_status` is `ok` only when the held-out check is under 2 m. San Francisco’s check is 20.84 m, so those rows stay on the Condor Club point. New York’s check is 3.33 m, so the projected curb is written and the status stays `uncertain`. The neighbourhood camera has no latitude, so its rows have null `lat`/`lon`. The second New York measurement is in `streetcam/out/geo_nyc_refine.json`. The full log is [streetcam/PROGRESS.md](streetcam/PROGRESS.md).

### Query

`query/api.py` serves the pages in `query/static/` and the JSON API. `query/store.py` reads the database. `query/sources.py` loads JSONL from `dashcam/out/` and `streetcam/out/` (or `KERB_JSONL_DIRS`). On the same key the database row wins. A bad JSONL line is skipped and counted on `GET /api/health`.

A pin query is a filter (state, side, source) plus a radius (`spaces_near`). Search then ranks those rows by cosine similarity. The embedder is Cosmos Embed1; `KERB_EMBEDDER=hash` is a test stand-in and is labelled as such in every response. Spaces with no lat/lon of their own are placed at their camera’s `kerb_cameras` position.

## The tables

Defined in `common/kerb_schema.py`. `common/kerbdb.py` rejects unknown fields and enum values before a write. Times are ISO-8601 UTC. `embedding` is 256 floats or null. `detail_json` is a JSON string.

| Table | One row is |
|---|---|
| `kerb_spaces` | One kerb stretch: `free`, `occupied`, `not_space`, or `cannot_tell`; side `L`, `R`, or `view`; position `ok` or `uncertain`; last seen; evidence; receipt text. |
| `kerb_observations` | One sighting of that stretch. |
| `kerb_trail` | One decision step: `gate`, `boxes`, `geometry`, `rule`, `cosmos`, `track`, `position`, `overrule`. |
| `kerb_overrules` | A person or agent correction, and which receipts it changed. |
| `kerb_eval_cases` | A frozen label (`dev` or `heldout`) and its sha256. |
| `kerb_cameras` | One camera: kind, place, `geo_source` (`pie_gps`, `landmark_estimate`, `refined`, `unknown`), and the homography check error. |

VastDB uses schema `kerbmap` in the team bucket (`KERB_VDB_SCHEMA` overrides it). The VSS collection tables are never written. Locally the file is `$KERB_SQLITE` or `~/kerb-data/kerb.sqlite`.

## Folders

| Path | What it is |
|---|---|
| [`common/`](common/README.md) | The table contract and the only database client. Tests in `common/tests/`. |
| [`dashcam/`](dashcam/RESULTS.md) | Builder A. Toronto PIE pipeline: clip list, keyframes, boxes, slots, OSM rules, Cosmos, receipts. `out/` holds the JSONL, evidence JPEGs, and per-set work files. |
| [`streetcam/`](streetcam/PROGRESS.md) | Builder B. Tracking, the parked rule, receipts, and the map fit for `neighborhood_cam-1`, `sf_streets_cam-5`, and `nyc_streets_cam-1`. `day-*/` holds per-camera tracks. `out/` holds receipts, timelines, and the geo fit. `frame_*.jpg` are the frames the kerb was drawn on. |
| [`query/`](query/README.md) | Builder C. API, map and receipt pages, labelling, replay, and MCP tools. |
| [`data/`](data/README.md) | GPS and place attached to every segment we use. `pie_map.csv` and `pie_segments.csv` are PIE’s own GPS, matched frame by frame. `video_geo.csv` is one row per segment for the dashcam and the three street cameras. |
| [`design/`](design/README.md) | Prototype mock data for slides. It does not feed the app. |
| [`deploy/`](query/DEPLOY.md) | Kubernetes manifest `k8s/kerbmap-app.yaml`. `query/build_deploy.py` writes a flat `deploy/app/` for the ConfigMap. Nothing in this folder has been applied to the cluster. |

Pages in `query/static/` (relative URLs only; `api.js` is the only file that calls the server):

| Page | What |
|---|---|
| `index.html` | Home: set up a dashcam, or find parking. |
| `setup.html` | How a dashcam syncs into the map. |
| `find.html` | Map. Pin, radius, and the receipts nearby. |
| `explore.html` | Same map with the receipt list. |
| `label.html` | Claim a batch and label a moment. |
| `board.html` | Labelling progress. |
| `measured.html` | Results of a measurement run. Empty until `GET /api/measured` has a file. |

## Getting started

Commands run from the repo root. Python 3.11 or newer.

The committed JSONL is enough to open the map. No lab login is required.

```bash
pip install -r query/requirements.txt
KERB_DB=local python -m query.api
```

Then open [http://localhost:8000/](http://localhost:8000/). Useful calls:

| Call | What |
|---|---|
| `GET /api/health` | Backend name, row counts, which JSONL files loaded. |
| `GET /api/spaces?lat=43.68&lon=-79.42&radius_m=500` | Receipts near a pin. |
| `GET /api/receipt/{space_id}` | Space, ordered trail, observations, camera, evidence. |
| `GET /api/search?q=free+kerb&lat=43.68&lon=-79.42` | Filter, then cosine rank. |
| `POST /api/overrule` | Body `{"space_id", "new_state", "note", "by"}`. |

Five synthetic rows, local SQLite only:

```bash
KERB_DB=local python -m query.seed_local
```

Tests (local backend only):

```bash
python -m pytest -q common/tests query/tests
```

MCP tools `find_free_space`, `get_receipt`, and `overrule`, over stdio. The five tasks, including an abstention, are in [query/agent_eval.md](query/agent_eval.md).

```bash
KERB_DB=local python -m query.mcp_server
```

### On a lab VM

Credentials live only in `/config/<team>.config`. Source them in your own shell. Do not print, log, or commit them. GitHub’s port 22 is blocked on the VM; use port 443.

```bash
KERB_DB=vastdb python -m query.api          # schema kerbmap; unset KERB_DB does this when /config/*.config exists
```

The real VastDB and Cosmos Embed1 check is [query/SMOKE_TEST.md](query/SMOKE_TEST.md). Deploy steps, for a person to run after the team agrees, are [query/DEPLOY.md](query/DEPLOY.md). Rehearse first:

```bash
bash query/rehearse_deploy.sh
```

Dashcam receipts need the detection cache, OSM cache, and frames outside this repo (`DASHCAM_DATA`, default path in `dashcam/receipts.py`):

```bash
python dashcam/receipts.py slots set04
python dashcam/receipts.py cosmos set04      # needs the local Cosmos server and frames
python dashcam/receipts.py write set04
```

Street-camera tracks need a detection cache from `streetcam/fetch_detections.py`, which logs into VSS. The parked rule and the map fit then run offline:

```bash
python streetcam/parked.py --kerb streetcam/kerb_neighborhood.json \
  --tracks streetcam/day-20260901/tracks.jsonl streetcam/day-20260902/tracks.jsonl \
  --out streetcam/out/occupancy_timeline.jsonl
python streetcam/geo.py                  # self-test
python streetcam/geo.py streetcam/geo_nyc.json   # print the fit; does not write receipts
```

`streetcam/apply_geo.py` copies a fit onto `kerb_spaces` and appends a `position` trail step. It rewrites the spaces file you pass it and appends to the trail file on every run. The files in `streetcam/out/` already have that step, so leave them unless you are rebuilding receipts from scratch:

```bash
python streetcam/apply_geo.py \
  streetcam/out/kerb_spaces.jsonl streetcam/out/kerb_trail.jsonl streetcam/out \
  streetcam/geo_sf.json streetcam/geo_nyc.json
```

## Where the numbers are

Every figure in a slide should come from one of these files, with its count beside it.

| File | What it records |
|---|---|
| [dashcam/RESULTS.md](dashcam/RESULTS.md) | The `receipts.py` run: slots, states, Cosmos share, time per stage. No accuracy score yet. |
| [dashcam/PROGRESS.md](dashcam/PROGRESS.md) | The earlier clip, box, and Cosmos run. |
| [streetcam/PROGRESS.md](streetcam/PROGRESS.md) | Tracks, occupancy, receipts, and both homography passes. |
| `streetcam/out/eval_labels.json` | Neighbourhood agreement on the labels that were open while the occupancy rule was fixed. That is a development score. |
| `streetcam/out/eval_heldout.json` | The held-out batches scored once after that fix. |
| `GET /api/measured` | The query measurement file, when someone writes it. The page is empty until then. |

## How we work

One branch per workstream: `dashcam/…`, `streetcam/…`, `query/…`, `design/…`. Small commits. A person reviews before anything lands on `main`.

Do not invent numbers, labels, or positions. Do not edit frozen labels. Do not re-ingest hours of video without the team agreeing. Do not ingest video from the internet. Cosmos runs only on gaps the cheaper steps could not settle.

Receipt crops must not show faces or number plates. Street-camera evidence frames blur plate regions. Store the intervals and the receipts. Raw video stays only as long as the library already keeps it.

## Credits

PIE dataset (York University, MIT licence), Ultralytics YOLO11 (AGPL-3.0), OpenStreetMap (ODbL), NVIDIA Cosmos, the VAST VSS blueprint, and the builders-challenge starter kit. Street names and the kerb reference nodes are © OpenStreetMap contributors.
