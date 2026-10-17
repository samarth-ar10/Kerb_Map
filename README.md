# Kerb Map

Dashcam and street-camera footage, turned into a map of kerb space. Each stretch has a receipt: `free`, `occupied`, `not_space`, or `cannot_tell`; which side; when it was last seen; an evidence frame; and a confidence score.

Open https://workshop.thecosmoslabs.com and click **App** (`/app` on the team host). The page on the cluster matches this repo. **Connect your dashcam**: pair once, clips sync to VAST and are processed into receipts.

Footage is the organisers' library: 612 indexed chunks. Toronto drives are `pie_cam-3` from the PIE dataset. Street cameras are `neighborhood_cam-1`, `sf_streets_cam-5`, and `nyc_streets_cam-1`. No raw faces or number plates are stored in saved frames.

[ONE-PAGER.md](ONE-PAGER.md) is the product sketch. [PLAN.md](PLAN.md) is the build plan. [AGENTS.md](AGENTS.md) is the working rules, including no faces or number plates in saved frames. [pitch/](pitch/) has the deck assets.

## Demo Video

Watch the short demo here: https://vimeo.com/1236839389?share=copy&fl=sv&fe=ci

## Architecture

A clip is stored, then VAST DataEngine runs the segmenter, YOLO11 boxes, Cosmos captions, and Cosmos Embed1. Kerb analysis uses those stored boxes (confidence at least 0.4, no track ids). It does not rerun vision on the whole video.

For each drive: a keyframe about every 8 m, parked-car rows and gaps, a lane check, OpenStreetMap rules, Cosmos only on gaps the rules leave open, then a GPS position. Rows go to VastDB schema `kerbmap`.

The six Toronto drives were written by `dashcam/receipts.py` and loaded into VastDB. A new upload is processed in the deployed app by `query/upload.py` and `query/kerbpipe.py`, with the same steps and the same schema.

![Repo architecture. VSS feeds data, dashcam, and streetcam. Those write through common/kerbdb.py. query reads the tables and the JSONL. deploy ships the app.](design/repo-architecture.png)

A free space is a legal stretch a camera saw, empty for about one car (5.5 m). A newer sighting of the same metres wins. Dashcam `position_status` is `ok` when the keyframe has PIE GPS. Street-camera `position_status` is `uncertain` unless a fixed location is known.

`common/kerb_schema.py` defines `kerb_spaces`, `kerb_observations`, `kerb_trail`, `kerb_overrules`, `kerb_eval_cases`, and `kerb_cameras`. Times are ISO-8601 UTC. `embedding` is 256 floats or null.

## Run it

Python 3.11 or newer, from the repo root. The committed JSONL is enough to open the map.

```bash
pip install -r query/requirements.txt
KERB_DB=local python -m query.api
```

Open [http://localhost:8080/](http://localhost:8080/). `PORT` overrides 8080.

| Call | |
|---|---|
| `GET /api/health` | Backend, row counts, which JSONL files loaded. |
| `GET /api/spaces?lat=43.68&lon=-79.42&radius_m=500` | Receipts near a pin. |
| `GET /api/receipt/{space_id}` | Space, trail, observations, camera, evidence. |
| `GET /api/search?q=free+kerb&lat=43.68&lon=-79.42` | Filter, then cosine rank. `KERB_EMBEDDER=hash` is the test stand-in. |
| `POST /api/overrule` | `{"space_id", "new_state", "note", "by"}`. |

```bash
KERB_DB=local python -m query.seed_local          # five TEST_ rows, local SQLite only
python -m pytest -q common/tests query/tests
KERB_DB=local python -m query.mcp_server          # find_free_space, get_receipt, overrule
```

GitHub Actions (`.github/workflows/test.yml`) compiles the Python packages, runs those tests, and checks the deploy bundle on every push and pull request. The run on main passed.

On a lab VM, credentials stay in `/config/<team>.config`. GitHub’s port 22 is blocked there; use 443.

```bash
KERB_DB=vastdb python -m query.api
```

Deploy: [query/DEPLOY.md](query/DEPLOY.md). Upload demo: [query/DEMO_UPLOAD.md](query/DEMO_UPLOAD.md). VastDB and Cosmos Embed1 check: [query/SMOKE_TEST.md](query/SMOKE_TEST.md). `kerbmap-app` is the deployed app name.

`streetcam/apply_geo.py` rewrites the spaces file you pass it and appends a `position` trail step on every run. The copies in `streetcam/out/` already have that step.

## Results

Counted on the day. The file in the last column is the source.

| What | Result | Source |
|---|---|---|
| Toronto dashcam | 1,080 segments x 5 s = 1.5 camera-hours, 6 drives | [dashcam/RESULTS.md](dashcam/RESULTS.md) |
| Frames kept by the distance gate | 2,701 of 162,000 (1.67%) | [dashcam/MEASURED-COST-AND-CANNOT-TELL.md](dashcam/MEASURED-COST-AND-CANNOT-TELL.md) |
| Dashcam receipts (`dc_`) | 5,110 from 7,846 sightings: 11 free, 1,710 occupied, 44 not a space, 3,345 cannot tell | [dashcam/RESULTS.md](dashcam/RESULTS.md) |
| Pie-receipts pipeline (`pr_`) | 5,302 receipts, converted into the same tables | [dashcam/RESULTS.md](dashcam/RESULTS.md) |
| Sightings sent to Cosmos | 4 of 7,846 (0.05%) | [dashcam/MEASURED-COST-AND-CANNOT-TELL.md](dashcam/MEASURED-COST-AND-CANNOT-TELL.md) |
| Cosmos vs captioning every 5 s segment | 2.67 vs 720 calls per camera-hour (270x fewer). 13.0 s vs 3,510 s of Cosmos time per camera-hour, from 4 uncached calls (4.81–4.99 s) on a team laptop | [dashcam/MEASURED-COST-AND-CANNOT-TELL.md](dashcam/MEASURED-COST-AND-CANNOT-TELL.md) |
| One 30 s clip to receipts | 0.23 s from stored detections. About 6.4 s if you add detection fetch and one Cosmos call | [dashcam/MEASURED-COST-AND-CANNOT-TELL.md](dashcam/MEASURED-COST-AND-CANNOT-TELL.md) |
| Why cannot tell | 5,482 of 5,516 cannot-tell sightings are "lane unclear" | [dashcam/MEASURED-COST-AND-CANNOT-TELL.md](dashcam/MEASURED-COST-AND-CANNOT-TELL.md) |
| Dashcam labels, shipped rules | 120 labels, 2 people, 108 spots all answered. `dc_` (81 spots): free 7 of 11, occupied 11 of 16, real kerb space 18 of 27. Where the model said cannot tell, peopl | [dashcam/RESULTS.md](dashcam/RESULTS.md) |
| Label-driven rules, held-out drives set04–set06 (25 spots; rules chosen on set01–set03) | Three changes together (gap 0.85, travel-lane row, OSM no-parking side): free 5 of 6 vs 4 of 5 shipp | [dashcam/RESULTS.md](dashcam/RESULTS.md) |
| Street-camera labels | 100 labels, 3 people. Neighbourhood held-out batches A1/A2/A4: 45 of 51 agree | `streetcam/out/eval_heldout.json` |
| Street-camera receipts | Neighbourhood 308 (82 occupied, 226 free). New York 360 (278 occupied, 79 free, 3 cannot tell) | [streetcam/PROGRESS.md](streetcam/PROGRESS.md) |
| Agent, gemma4:e4b via Ollama | 5 of 5 tasks match the API, including cannot-tell for central London and a stored overrule | [query/out/agent_run.md](query/out/agent_run.md) |
| Agent, Qwen2.5-1.5B local | 2 of 5 | `query/out/agent_transcript.json` |
| Stock caption search | "free parking space at the kerb": none of 5 target chunks in the top 15 | day-of re-ingest record (not in this repo) |

`query/agent_run.py --llm wandb` is the same tasks against W&B Serverless Inference. The key is `WANDB_API_KEY`, not a file in the repo.

## Limits

- 11 of 5,110 dashcam receipts are free. A drive-by sees parked cars much more often than a legal empty gap. Street cameras watch one kerb for minutes.
- The 120 dashcam labels are a sample (all frees and not-a-spaces, plus a spread of the rest), from 2 people. The two people agreed on 6 of 8 shared spots; both disagreements are free vs not a spa
- 3,345 of 5,110 dashcam receipts are cannot tell, mostly cars on the left of two-way roads, where an oncoming lane may sit between the camera and the kerb.
- San Francisco is held back. The homography check is 20.84 m, and a spot check found free receipts with a car in the strip. Those rows are in `streetcam/out/sf/held_back/`.
- New York positions are `uncertain`. The held-out map check is 3.33 m, against a 2 m bar. The neighbourhood camera has no latitude or longitude.
- Dashcam pin error is not measured. Pins use an uncalibrated focal length and carry an along-road error of several metres.
- Cosmos timings above are from a team laptop. The event endpoint's per-call time is not measured.

## Layout

| Path | What it is |
|---|---|
| [`common/`](common/README.md) | Table contract and the only database client. |
| [`dashcam/`](dashcam/RESULTS.md) | Toronto pipeline (`dc_` ids). `convert_teammate.py` loads the pie-receipts run (`pr_` ids). |
| [`streetcam/`](streetcam/PROGRESS.md) | Tracker, parked rule, hand-drawn kerb, homography, receipts. |
| [`query/`](query/README.md) | API, pages, upload, labelling, MCP tools, agents. |
| [`weave_eval/`](weave_eval/comparison.md) | Rule variants against the human labels. |
| [`data/`](data/README.md) | PIE GPS and one geo row per segment. |
| [`design/`](design/README.md) | The figure above, plus slide mock data the app does not read. |
| [`deploy/`](query/DEPLOY.md) | `query/build_deploy.py` and `k8s/kerbmap-app.yaml`. |

Pages in `query/static/` use relative URLs. `api.js` is the only file that calls the server.

## Credits

PIE dataset (York University, MIT licence). OpenStreetMap (ODbL); street names and kerb reference nodes are © OpenStreetMap contributors. Ultralytics YOLO11 (AGPL-3.0). NVIDIA Cosmos. VAST Data.