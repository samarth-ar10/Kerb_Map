# data/

## pie_map.csv and pie_segments.csv: GPS for the Toronto clips

The library's Toronto clips (`pie_cam-3`, 180 chunks of 30 s, 1,080 segments of 5 s) come from the public PIE dashcam
dataset. The index stores no GPS, so each chunk and segment was matched frame against frame to its PIE video, and PIE's
own vehicle GPS/OBD annotations were attached.

- `pie_map.csv`: one row per chunk. `pie_video` and `start_s`/`end_s` give the exact time in the PIE video
  (`offset_correction_s` is how far the chunk start sits from 30 × chunk; it varies 0 to −8 s, so never assume
  `s // 30`). Then GPS start/end, mean heading and speed, distance, `stationary`, main street and all streets, and a
  match `confidence` (high/medium/low; medium and low are stopped or creeping cars).
- `pie_segments.csv`: one row per 5 s segment, keyed by `segment_source` (the S3 URI in the index), with its own
  GPS start/end, heading, speed, distance and streets.
- Chunks 0–19 are PIE `video_0001` and 20–29 are `video_0002` (set04: 0–17 and 18–29).
- GPS is about 1 Hz. Street names come from OpenStreetMap; some roads are unnamed.

Licence: PIE dataset, MIT, York University (Rasouli et al., ICCV 2019). Only derived timestamps and coordinates are stored
here; no PIE video is redistributed. Street names © OpenStreetMap contributors (ODbL).
