# query/ progress (Builder C)

## Done today
1. `common/`: six-table contract (`kerb_schema.py`, incl. `kerb_cameras`) and `kerbdb.py` (local SQLite and VastDB
   backends, row validation, `spaces_near` with camera-position fallback). 12 unit tests, local backend.
2. `query/api.py`: health, spaces (pin/radius/filters, `as_of` for replay), receipt (ordered trail, overrules,
   observations, camera, evidence + clip timing, `query_explain`), overrule (before/after), evidence pass-through,
   measured (404 until a results file exists). JSONL fallbacks from `dashcam/out`, `streetcam/out` are merged in.
3. `query/search.py` + `api/search`: DB filter + radius, then cosine rank with Cosmos Embed1 (code reads the GPU
   settings at runtime). `hash` embedder is test-only and labelled as such in every response.
4. `query/labels.py`: batch claim (20 items, 15 min locks, priority order, ~1 in 10 overlap, third look,
   held-out pool), append-only labels (latest per person wins), release, board, summary (dev only), frozen export.
5. `query/replay.py`: replays existing receipts clip by clip from the chunk timeline; every response says "replay".
6. `query/mcp_server.py`: `find_free_space`, `get_receipt`, `overrule` over stdio; `agent_eval.md` lists 5 tasks
   incl. a "cannot tell" abstention. No live LLM run.
7. Front end from the design workstream copied flat into `query/static/`; API shapes aligned to its README
   (reasons list, `evidence_start`/`evidence_end`, board/summary/replay shapes). Unbuilt optional routes return 404.
8. Deploy: `build_deploy.py` (flat `deploy/app/`, size and relative-URL checks), `rehearse_deploy.sh`,
   `deploy/k8s/kerbmap-app.yaml`, `DEPLOY.md` (commands for a person; nothing applied to the cluster).

## What ran (all on TEST rows in a local SQLite file; no VastDB, GPU or VSS calls)
- `python -m pytest -q common/tests query/tests`: 31 passed on the laptop (Python 3.11) and on the lab VM (3.12).
- `bash query/rehearse_deploy.sh`: PASS on the laptop and on the VM (20 files, ~200 KB, `python main.py` on 8080
  behind a local `/app` prefix proxy; all pages, assets and API calls 200; preview-data.json, api/measured,
  api/live, api/coverage, api/geocode 404).
- Lab VM: app running from a scratch copy on port 8000 against the local backend with 5 TEST spaces; health, pages,
  spaces, board and replay drives return 200. MCP stdio server lists the three tools and answers find_free_space.

## Not done / needs a person
- Real VastDB and Cosmos Embed1 check: `SMOKE_TEST.md` (not run; needs the team credentials).
- Deploy to the cluster: `DEPLOY.md` (not run; lead's rule).
- `api/live`, `api/coverage`, `api/geocode` not built (pages fall back). `api/measured` waits for a measurement run.
- The S3 presign fallback in `evidence.py` and the VSS stream pass-through are untested (need credentials).
- Batch locks are in memory (one replica); a restart frees open batches. Labels themselves are durable rows.
