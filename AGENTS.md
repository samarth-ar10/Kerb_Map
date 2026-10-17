# Agent guide: Team 5, Kerb Map

Read this before any task. The full plan is [PLAN.md](PLAN.md).

## What we're building
Kerb Map: on-street parking from dashcams first, then street cameras. Each stretch of kerb gets a **receipt**: legal
space or not, free / occupied / cannot tell, position or "position uncertain", last seen, evidence frames and the
decision trail. A pin or postcode query returns the receipts nearby.

## Where things are
- Each teammate has their own lab VM (Ubuntu, 8 CPU, 10 GB RAM, **no GPU**). Shared by the whole team: this GitHub repo
  and the team's VSS instance (index, VastDB, S3 buckets, models).
- Starter-kit skills on every VM: `~/vast-builders-challenge/.cursor/skills/` (retrieval: login, explore, videos,
  detections, search, agent-qa; ingest: reingest-videos, reingest-chunk; vast-database: vastdb-read, vastdb-write;
  gpu: model health; deployment: deploy-app-no-registry). Use them instead of guessing endpoints.
- Credentials live only in `/config/<team>.config` on the VM. Source them in your own shell. **Never print, log, copy
  or commit them.**

## The data (checked)
- 612 indexed chunks, 30 s each, 6 segments of 5 s. Toronto `pie_cam-3`: 180 chunks = PIE set01–set06, chunk
  0000–0029. Street cameras: `neighborhood_cam-1` (52, two days, day/fog/night), `sf_streets_cam-5`,
  `nyc_streets_cam-1`.
- Each segment row has location (one word), camera_id, caption, and `detection_sidecar_uri`: YOLO11 boxes for **every
  frame** (30 fps, 1920×1080, conf ≥ 0.4, no track ids). Read these boxes; don't re-run YOLO.
- **No GPS or capture clock in the index.** Toronto GPS comes from PIE's own files, mapped per chunk in
  `data/pie_map.csv` (when added). Without it, write "position uncertain".

## Rules
- Never invent numbers, labels or results. Every number shown must come from a file a teammate can re-run.
- Labels in `data/labels/` are frozen. Don't edit them; add new label files with a date.
- Re-ingest only a few clips to test. Larger re-ingests need the team's OK (it is slow and shared).
- Don't ingest internet video (organiser rule).
- No faces or number plates in outputs or crops.
- Cosmos is called only on unclear cases (cheap steps first). Keep concurrency at 1–2; the GPU is shared.
- Parked (street cameras): still for more than about 2 minutes **and** on the kerb strip. Cars at lights stand still too.

## How we work
- Branch per workstream: `dashcam/…` (Builder A), `streetcam/…` (Builder B), `query/…` (Builder C), `design/…`.
  Small commits. A person reviews before anything goes to `main`.
- Shared contract: the five VastDB tables `kerb_spaces`, `kerb_observations`, `kerb_trail`, `kerb_overrules`,
  `kerb_eval_cases` (PLAN.md section 3). Write to them; don't invent other formats.
- Split big jobs across sub-agents (one per PIE set or per camera).
- End every task with a short note in the PR or commit: what ran, on which clips, what came out, what failed.
- Git on the VM goes over port 443 (port 22 to GitHub is blocked).
