# On-stage demo: upload a dashcam clip, see the kerb results

Flow: **Connect dashcam → upload a sample clip → it is processed automatically → enter a location → see the kerb
results.** Code: `query/upload.py` (routes and job), `query/kerbpipe.py` (kerb pipeline, same steps and constants as
`dashcam/receipts.py`), `query/samples_gps.json` (GPS of the prepared clips, from `query/make_samples_gps.py`).

## The clips

`day-of/demo-samples/` (copy to the stage laptop's local disk and a USB stick). 30 s each, 1920x1080, ~23 MB, cut from
the supplied Toronto PIE drives. Their GPS is already in the app (`samples_gps.json`), so the file alone is enough:
the server recognises the file name (or the UI sends `sample_id` 1, 2 or 3).

| sample_id | File | Street |
|---|---|---|
| 2 (first) | `kerbmap_sample_2_augusta_ave.mp4` | Augusta Avenue |
| 1 (backup) | `kerbmap_sample_1_dundas_st_w.mp4` | Dundas Street West |
| 3 (spare) | `kerbmap_sample_3_college_st.mp4` | College Street |

The expected counts in `samples.json` come from the batch pipeline (two pipelines merged) and will not match the
upload counts exactly. The dry run below gives the real numbers for each clip; say those.

## Stage steps and what each shows

1. **Connect dashcam** (setup page): pick the clip file. The browser shows its upload progress to our app.
2. **Upload to VAST**: the app sends the clip to the team VSS (`POST /api/v1/videos/upload`, public, tag
   `kerbmap-demo`, camera `dashcam-demo`, location `toronto`, our kerb prompt). Detail: size and the VAST object key.
3. **Indexed by VAST (YOLO + Cosmos)**: VSS cuts the clip into 5 s segments, runs YOLO on every frame, captions each
   segment with Cosmos, embeds it and writes it to VastDB. Detail counts up: "3 of 6 segments indexed (40 s)".
4. **Kerb analysis**: the app reads VSS's stored boxes for each segment and runs our steps: keyframe every 8 m of
   travel, parked-car rows and gaps, lane check (a car whose lane is unclear is "cannot tell", never guessed), the
   OpenStreetMap kerb rules (junction, driveway, crossing, hydrant, bus stop; map data for the three clips is shipped
   with the app), Cosmos only for a gap the rules cannot settle (else "cannot tell: model unavailable"), and a position
   from the clip's GPS. Detail: keyframes, kerb sightings, map source, model checks.
5. **Saved to VastDB**: receipts, observations and the decision trail go to the `kerbmap` tables. The job ends with
   counts by state (free / occupied / not a space / cannot tell) and a map centre.
6. **Enter a location**: type the street (e.g. Augusta Avenue) or use the centre the job returned; the map shows that
   job's receipts (`api/spaces?job_id=...`). Click a receipt for its trail: gate, boxes, geometry, rule, position.

## Timings

| Step | Local lab (simulated VSS) | Real system |
|---|---|---|
| Browser → app (23 MB) | under 1 s | depends on the venue network, measure in the dry run |
| Upload to VAST | under 1 s | measure in the dry run (inside the cluster) |
| Indexed by VAST | 12 s (simulated delay) | tens of seconds to minutes (starter kit); **timeout 6 min**, then the job turns red with a clear message |
| Kerb analysis | about 1 s | 6 detection reads + about 1 s; plus about 5 s per model check, at most 3 |
| Saved to VastDB | under 1 s | a few seconds (3 inserts) |

Lab runs (`.\lab.ps1 start`, app on the lab): samples 1, 2, 3 each finished in 15-17 s. Real-system timings are not
measured yet: write them here after the dry run.

## Fallback (indexing slow or the network fails)

- **Prepared job:** after the dry run, keep its job id. `GET api/upload/latest-done` returns it (or set it explicitly:
  `KERB_DEMO_JOB=<job_id>`, in the app's Secret on the cluster). A finished job keeps answering after a restart: it is
  rebuilt from its saved receipts. The UI shows that job's steps and results straight away.
- **Sample already in VAST:** sending `sample_id` without a file reuses the newest indexed copy of that sample in VAST
  (step 1 says "already in VAST"), so only the kerb analysis runs (a few seconds).
- If a job ends in `error`, the message says what failed (upload refused, indexing timeout, no detections).

## Dry run on the VM against the real system (a person runs this)

This uploads one 30 s clip into the team's shared VSS index (public, tag `kerbmap-demo`) and writes receipts
(`space_id` starting `up_`, camera `dashcam-demo`) into the `kerbmap` tables. Agree it with the team first.

On the lab VM, from the repo root (credentials are read from `/config/<team>.config` by the app, never printed):

```bash
git fetch origin && git checkout query/main && git pull --ff-only
pip install --user -r query/requirements.txt          # once; same packages as SMOKE_TEST.md
python3 query/build_deploy.py && cd deploy/app && KERB_DB=vastdb PORT=8000 python3 main.py
```

From the laptop: `ssh -L 8000:localhost:8000 bc-samarthar10`, open `http://localhost:8000/setup.html`, click Upload and
pick `kerbmap_sample_2_augusta_ave.mp4`. Progress also at `http://localhost:8000/api/upload/<job_id>`. Optional model
check on unclear gaps: start the app with `COSMOS3_REASON_URL=<Cosmos endpoint> GPU_BEARER_TOKEN=<from the config>`
in front of `python3 main.py`; without them those gaps say "model unavailable".

On the cluster the same code runs from the ConfigMap (`DEPLOY.md`, "Update after a code change"); the Secret may also
carry optional `COSMOS3_REASON_URL` and `KERB_DEMO_JOB`.

## Local lab (no real system touched)

`day-of/local-lab/vss_sim.py` has a lab-only `POST /api/v1/videos/upload`: it discards the file and, after 6 s, lists
a new "mine" video whose segments are a cached PIE chunk's real recorded segments (one per second). Then:

```powershell
.\lab.ps1 start; . .\lab_env.ps1
cd ..\..\wt\query; $env:KERB_UPLOAD_JOBS="$env:TEMP\upload_jobs.json"; & $LabPy -m uvicorn query.api:app --port 8000
curl.exe -F "file=@..\..\day-of\demo-samples\kerbmap_sample_2_augusta_ave.mp4" http://127.0.0.1:8000/api/upload
```

Tests: `python -m pytest -q query/tests/test_upload.py` (pipeline on recorded detections, job states with VSS faked).
