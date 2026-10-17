# Smoke test against the real VastDB and the Cosmos Embed1 endpoint

Run by a person on the lab VM, from the repo root, once the team is happy to create the `kerbmap` schema in the team
bucket. It reads credentials at runtime from `/config/<team>.config` (nothing is printed), creates the six tables if
missing (schema `kerbmap`, never `vss-schema`/`vss-collection`), writes one clearly marked TEST row to
`kerb_spaces`, reads it back with a filter and a radius query, embeds one query with Cosmos Embed1 (expects 256 dims)
and ranks the TEST row. The TEST row is left in place with `receipt_text` starting `TEST:` and `space_id`
`TEST_SMOKE_<time>` so it is easy to find and filter out.

```bash
pip install -r query/requirements.txt        # vastdb, pyarrow, fastapi, ...
KERB_DB=vastdb KERB_EMBEDDER=cosmos python3 - <<'EOF'
import time
from common import kerbdb
from query import search

db = kerbdb.get_db()
print("backend:", db.backend, "bucket schema:", db.schema_name)
db.ensure_tables()
sid = f"TEST_SMOKE_{int(time.time())}"
vec = search.embed_query("free kerb space between parked cars")
print("embedding dims:", len(vec))
db.upsert_space({"space_id": sid, "source": "dashcam", "side": "L", "state": "free", "lat": 43.68296,
                 "lon": -79.42049, "position_status": "ok", "confidence": 0.5,
                 "receipt_text": "TEST: smoke test row, not a result", "embedding": vec})
db.upsert_space({"space_id": sid, "reason": "TEST smoke upsert"})
rows = db.read("kerb_spaces", {"space_id": sid}, limit=5)
print("read back:", len(rows), rows[0]["state"], rows[0]["reason"])
near = db.spaces_near(43.68296, -79.42049, radius_m=50, state="free")
print("spaces_near hit:", any(r["space_id"] == sid for r in near), "rows near:", len(near))
ranked, name = search.rank("free kerb space", near)
print("ranked with", name, "top:", ranked[0][1]["space_id"] if ranked else None, ranked[0][0] if ranked else None)
EOF
```

Expected: `backend: vastdb`, `embedding dims: 256`, `read back: 1 free TEST smoke upsert`, `spaces_near hit: True`,
`ranked with cosmos`.

Then the app on the VM against VastDB (port 8000):
```bash
KERB_DB=vastdb python3 -m query.api        # PORT defaults to 8080; set PORT=8000 to use 8000
curl -s localhost:8080/api/health
```
