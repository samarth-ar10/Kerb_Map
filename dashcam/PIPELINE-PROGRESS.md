# Dashcam receipts: progress

Branch `dashcam/receipts`. Code in `dashcam/`, outputs in `dashcam/out/`, counts in `RESULTS.md`.

| Step | Status | What ran / came out |
|---|---|---|
| 0 Setup | done | Worktree from origin/main + origin/query/main (common/kerbdb.py). |
| 1 Detections | done | `fetch_detections_vm.py` on the VM: 1,080 / 1,080 segments, 235 s, copied to bystander-data/dashcam-det (JSON only). |
| 2 OSM | done | `osm_fetch.py`: one Overpass request per drive, 6 / 6 cached (set06 needed retries on a mirror). |
| 3 Keyframes | done | Distance gate, 8 m: 2,701 keyframes from 162,000 frames (1.67%). |
| 4 Boxes + geometry | done | car/truck/bus >= 0.4; lane check (traffic / parked / lane unclear); gaps from box widths. 7,846 slots. |
| 5 Rules | done | OSM junction, driveway, crossing, signals, hydrant, bus stop radii; 10 gaps rejected. |
| 6 Cosmos | done | 4 / 7,846 slots (0.05%) sent; frames cut on the VM (`vm_frames.py`, `frames.py`). |
| 7 Position | done | GPS interpolated at keyframe time + forward/side offset; not snapped to the road yet. |
| 8 Write | done | 5,110 receipts (`dc_` ids), 7,846 observations, 25,554 trail rows in the lab SQLite; JSONL per drive. |
| 9 Label pool | done | `out/label_candidates.csv`: 120 receipts (free, rule-rejected, model-checked, cannot_tell first). |
| 10 Teammate convert | done | `convert_teammate.py`: 5,302 pie-receipts rows -> contract, all positioned, 0 dropped (`pr_` ids). |

Order of the first ship: set04 end to end first, then set01-03, set05, set06 (set06 waited for OSM).

## Re-run

```
python dashcam/receipts.py slots set01 set02 set03 set04 set05 set06
python dashcam/receipts.py requests set01 ...    # boxed-frame list for the queued gaps
python dashcam/frames.py set01 ...               # frames from the VM
python dashcam/receipts.py cosmos set01 ...
python dashcam/receipts.py write set01 ...       # KERB_DB=local, KERB_SQLITE defaults to the lab file
python dashcam/label_candidates.py
python dashcam/convert_teammate.py
```
Use the lab venv (`bystander-data/local-lab/venv`, has pyarrow). Re-runs replace only their own rows.

## Open
- Lane unclear makes 70% of slots cannot_tell; tracking across keyframes or road segmentation would settle many.
- Forward distance uses an assumed focal length; pins not snapped to the road.
- No accuracy until the label batches come back.
