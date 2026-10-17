# common/: shared table contract and database access

`kerb_schema.py` defines the six tables (`kerb_spaces`, `kerb_observations`, `kerb_trail`, `kerb_overrules`,
`kerb_eval_cases`, `kerb_cameras`) as pyarrow schemas, with keys and allowed enum values. `kerbdb.py` reads and
writes them on VastDB (schema `kerbmap` in the team bucket, data VIP) or on a local SQLite file.

```python
from common import kerbdb                       # run from the repo root
db = kerbdb.get_db()                            # KERB_DB=vastdb|local (default: vastdb if /config/*.config exists)
db.ensure_tables()                              # creates missing tables, never touches vss-collection
db.upsert_space({"space_id": "set01_0001_L_003", "source": "dashcam", "side": "L", "state": "free",
                 "lat": 43.683, "lon": -79.4205, "position_status": "ok", "confidence": 0.82})
db.insert("kerb_trail", [{"trail_id": "set01_0001_L_003_1", "space_id": "set01_0001_L_003", "step_no": 1,
                          "step": "gate", "detail_json": "{}", "at": "2026-10-17T13:00:00Z"}])
db.read("kerb_spaces", {"state": "free"}, limit=50)          # equality filters; a list means IN
db.spaces_near(43.683, -79.4205, radius_m=150, state="free")  # adds distance_m, sorted nearest first
```

- Rows are validated: unknown fields, a missing key, enum values outside the contract and wrong types raise `SchemaError`.
- Times are ISO-8601 UTC strings; `embedding` is a 256-float list or null; `detail_json` is a JSON string.
- Spaces without their own lat/lon (e.g. a fixed street-camera view) are placed at their camera's `kerb_cameras`
  position by `spaces_near` (`position_from: "camera"`).
- Local: `KERB_DB=local`, file `$KERB_SQLITE` or `~/kerb-data/kerb.sqlite`. VastDB settings come from env
  (`S3_ENDPOINT`, `ACCESS_KEY`, `SECRET_KEY`, `VASTDB_BUCKET`) or the single `/config/*.config`, read at runtime only.
- Tests (local backend only): `python -m pytest -q common/tests`.
