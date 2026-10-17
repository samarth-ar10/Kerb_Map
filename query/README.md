# query/: Kerb Map app, API, labelling, replay and MCP tools

Run from the repo root.

```bash
pip install -r query/requirements.txt
KERB_DB=local python -m query.seed_local                 # optional: 5 TEST rows (local SQLite only)
KERB_DB=local KERB_EMBEDDER=hash PORT=8000 python -m query.api
# pages: http://localhost:8000/ (map), label.html, board.html, measured.html
python -m pytest -q common/tests query/tests             # local backend only
bash query/rehearse_deploy.sh                            # deploy rehearsal, see DEPLOY.md
```

Data: rows from the database (`common.kerbdb`, `KERB_DB=local|vastdb`) plus the builders' JSONL fallbacks in
`dashcam/out/*.jsonl` and `streetcam/out/*.jsonl` (or `KERB_JSONL_DIRS`). A JSONL line is one table row; the table
comes from a `"table"` field or the file name (`kerb_spaces.jsonl`, `kerb_trail_set01.jsonl`, `set01_kerb_spaces.jsonl`). Bad lines are
skipped and counted in `api/health`. On the same key the database row wins.

Evidence drawing: a trail step's `detail_json` may carry `"box": [x1, y1, x2, y2]` or `"point": [x, y]` in frame
pixels, plus `"frame_size": [w, h]`; the pages draw it on the evidence frame. Receipts and batch items carry
`evidence_start`/`evidence_end` (`clip_start`/`clip_end`): seconds within the evidence video, from `t_sec` read as
seconds into `original_video` (shifted for a 5 s segment file). Images have no timing.

Pages (`static/`): the front end from the design workstream, flat files with relative URLs only. `api.js` is the only
file that calls the server. `preview-data.json` is not part of the app; without it, an unreachable API shows an empty
page with a banner.

| Route | What |
|---|---|
| `GET health`, `GET api/health` | liveness; backend, row counts, JSONL files |
| `GET api/spaces?lat&lon&radius_m&state&side&source&as_of` | receipts near a pin (or all); `as_of` = replay seconds |
| `GET api/receipt/{space_id}?q=` | space, ordered trail, overrules, observations, camera, evidence, `query_explain` |
| `POST api/overrule` | `{space_id, new_state, note, by}` → before/after; adds an overrule trail step |
| `GET api/search?q&lat&lon&radius_m&state&side&source` | DB filter + radius, then cosine rank (Cosmos Embed1; `hash` is test-only) |
| `GET api/evidence?uri=` | frame or clip passed through (s3 via VSS stream, local files for development) |
| `POST api/batches/claim`, `POST api/batches/{id}/release` | 20-item labelling batches, 15 min locks |
| `POST api/labels` | append-only label; re-label by the same person supersedes |
| `GET api/board`, `GET api/labels/summary`, `GET api/labels/export?split=&format=` | progress, human vs pipeline (dev), frozen export |
| `GET api/measured` | results JSON from the measurement run (`results/measured.json` or `KERB_MEASURED`); 404 = not measured yet |
| `GET api/replay/drives`, `POST api/replay/start`, `GET api/replay/status`, `POST api/replay/stop` | replay existing receipts clip by clip |
| `POST api/upload` (multipart `file`, `gps`, `sample_id`), `GET api/upload/{job_id}` | upload a dashcam clip to VAST, wait for indexing, run the kerb pipeline (`kerbpipe.py`), save receipts `up_<job_id>_...`; see `DEMO_UPLOAD.md` |
| `GET api/samples` (= `api/upload/samples`), `GET api/upload/latest-done` | prepared demo clips with known GPS (`samples_gps.json`, from `make_samples_gps.py`); the fallback job |
| `GET api/spaces?job_id=` | only the receipts of one upload job (receipts carry `job_id`) |

MCP (stdio): `python -m query.mcp_server` with `find_free_space`, `get_receipt`, `overrule`. Tasks: `agent_eval.md`.
Real VastDB/embedding check: `SMOKE_TEST.md`. Deployment: `DEPLOY.md`.
