"""Read and write the Kerb Map tables, on VastDB or on a local SQLite file.

    from common import kerbdb
    db = kerbdb.get_db()            # backend from env KERB_DB (vastdb | local)
    db.ensure_tables()
    db.insert("kerb_trail", [{...}])
    db.upsert_space({"space_id": "...", "state": "free", ...})
    db.read("kerb_spaces", {"state": "free"}, limit=50)
    db.spaces_near(43.68, -79.42, radius_m=150, state="free")

Backend choice:
- KERB_DB=local  -> SQLite at $KERB_SQLITE or ~/kerb-data/kerb.sqlite
- KERB_DB=vastdb -> VastDB, schema `kerbmap` (override: KERB_VDB_SCHEMA) in the team bucket
- unset          -> vastdb when the team config is found, else local

VastDB credentials are read at runtime from the environment (S3_ENDPOINT, ACCESS_KEY,
SECRET_KEY, VASTDB_BUCKET) or, if missing, from the single /config/*.config file.
They are never logged. Only the data VIP (S3_ENDPOINT) is used, and the VSS tables
(vss-collection etc.) are never touched.
"""

import glob
import json
import math
import os
import sqlite3
import threading

from common.kerb_schema import KEYS, TABLES, SchemaError, to_arrow, validate_row

VDB_KEYS = ("S3_ENDPOINT", "ACCESS_KEY", "SECRET_KEY", "VASTDB_BUCKET")
DEFAULT_VDB_SCHEMA = "kerbmap"
EARTH_R_M = 6371000.0


# ---------------------------------------------------------------- helpers

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


def bbox(lat, lon, radius_m):
    dlat = radius_m / 111320.0
    dlon = radius_m / (111320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


def _team_config_path():
    paths = sorted(glob.glob("/config/*.config"))
    return paths[0] if len(paths) == 1 else None


def load_vastdb_config() -> dict:
    """Return the VastDB settings from env, falling back to the team config file (never sourced)."""
    cfg = {k: os.environ.get(k) for k in VDB_KEYS}
    missing = [k for k, v in cfg.items() if not v]
    path = _team_config_path()
    if missing and path:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if "=" not in line or line.startswith("#"):
                    continue
                key, value = line.split("=", 1)
                key = key.removeprefix("export ").strip()
                if key in missing and not cfg.get(key):
                    cfg[key] = value.strip().strip('"').strip("'")
    still_missing = [k for k, v in cfg.items() if not v]
    if still_missing:
        raise RuntimeError(f"VastDB settings missing: {still_missing} (set env or /config/<team>.config)")
    return cfg


def _space_matches(row, state, side, source):
    return ((state is None or row.get("state") == state)
            and (side is None or row.get("side") == side)
            and (source is None or row.get("source") == source))


def _near(db, lat, lon, radius_m, state, side, source, limit):
    """Shared spaces_near logic on top of a backend's _read_box()."""
    lat0, lat1, lon0, lon1 = bbox(lat, lon, radius_m)
    eq = {k: v for k, v in (("state", state), ("side", side), ("source", source)) if v is not None}
    out = []
    for row in db._read_box("kerb_spaces", lat0, lat1, lon0, lon1, eq):
        if row.get("lat") is None or row.get("lon") is None:
            continue
        d = haversine_m(lat, lon, row["lat"], row["lon"])
        if d <= radius_m:
            out.append({**row, "distance_m": round(d, 1), "position_from": "space"})

    # Spaces without their own position (e.g. a fixed street camera view) use the camera position.
    seen = {r["space_id"] for r in out}
    for cam in db._read_box("kerb_cameras", lat0, lat1, lon0, lon1, {}):
        if cam.get("lat") is None or cam.get("lon") is None:
            continue
        d = haversine_m(lat, lon, cam["lat"], cam["lon"])
        if d > radius_m:
            continue
        for row in db.read("kerb_spaces", {"camera_id": cam["camera_id"], **eq}, limit=10000):
            if row["space_id"] in seen or row.get("lat") is not None:
                continue
            out.append({**row, "lat": cam["lat"], "lon": cam["lon"], "distance_m": round(d, 1),
                        "position_from": "camera"})
            seen.add(row["space_id"])

    out.sort(key=lambda r: r["distance_m"])
    return out[:limit]


# ---------------------------------------------------------------- local (SQLite)

_SQL_TYPES = {"string": "TEXT", "int32": "INTEGER", "int64": "INTEGER", "double": "REAL", "float": "REAL"}


def _is_list(field):
    import pyarrow as pa
    return pa.types.is_list(field.type)


class LocalDB:
    """SQLite backend for tests and offline work. Same API as VastDB backend."""

    backend = "local"

    def __init__(self, path=None):
        path = path or os.environ.get("KERB_SQLITE") or os.path.expanduser("~/kerb-data/kerb.sqlite")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def ensure_tables(self):
        with self._lock, self._conn:
            for name, schema in TABLES.items():
                cols = []
                for f in schema:
                    sql_type = "TEXT" if _is_list(f) else _SQL_TYPES[str(f.type)]
                    pk = " PRIMARY KEY" if f.name == KEYS[name] else ""
                    cols.append(f'"{f.name}" {sql_type}{pk}')
                self._conn.execute(f'CREATE TABLE IF NOT EXISTS {name} ({", ".join(cols)})')

    def _encode(self, table, row):
        out = {}
        for f in TABLES[table]:
            v = row.get(f.name)
            out[f.name] = json.dumps(v) if (_is_list(f) and v is not None) else v
        return out

    def _decode(self, table, sql_row):
        row = dict(sql_row)
        for f in TABLES[table]:
            if _is_list(f) and row.get(f.name) is not None:
                row[f.name] = json.loads(row[f.name])
        return row

    def insert(self, table, rows):
        if not rows:
            return 0
        to_arrow(table, rows)  # full validation incl. types
        names = TABLES[table].names
        sql = f'INSERT INTO {table} ({", ".join(chr(34) + n + chr(34) for n in names)}) ' \
              f'VALUES ({", ".join("?" for _ in names)})'
        with self._lock, self._conn:
            self._conn.executemany(sql, [[self._encode(table, r)[n] for n in names] for r in rows])
        return len(rows)

    def upsert_space(self, row):
        """Insert or merge a kerb_spaces row by space_id. Returns the stored row."""
        validate_row("kerb_spaces", row)
        existing = self.read("kerb_spaces", {"space_id": row["space_id"]}, limit=1)
        merged = {**(existing[0] if existing else {}), **row}
        to_arrow("kerb_spaces", [merged])
        names = TABLES["kerb_spaces"].names
        enc = self._encode("kerb_spaces", merged)
        sql = f'INSERT OR REPLACE INTO kerb_spaces ({", ".join(chr(34) + n + chr(34) for n in names)}) ' \
              f'VALUES ({", ".join("?" for _ in names)})'
        with self._lock, self._conn:
            self._conn.execute(sql, [enc[n] for n in names])
        return merged

    def _where(self, table, filters):
        clauses, params = [], []
        for col, value in (filters or {}).items():
            if col not in TABLES[table].names:
                raise SchemaError(f"{table}: unknown filter column {col!r}")
            if isinstance(value, (list, tuple, set)):
                value = list(value)
                clauses.append(f'"{col}" IN ({", ".join("?" for _ in value)})')
                params.extend(value)
            elif value is None:
                clauses.append(f'"{col}" IS NULL')
            else:
                clauses.append(f'"{col}" = ?')
                params.append(value)
        return clauses, params

    def read(self, table, filters=None, limit=1000):
        if table not in TABLES:
            raise SchemaError(f"unknown table {table!r}")
        clauses, params = self._where(table, filters)
        sql = f"SELECT * FROM {table}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, params + [int(limit)]).fetchall()
        return [self._decode(table, r) for r in rows]

    def _read_box(self, table, lat0, lat1, lon0, lon1, eq):
        clauses, params = self._where(table, eq)
        clauses += ['"lat" BETWEEN ? AND ?', '"lon" BETWEEN ? AND ?']
        params += [lat0, lat1, lon0, lon1]
        sql = f"SELECT * FROM {table} WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._decode(table, r) for r in rows]

    def spaces_near(self, lat, lon, radius_m, state=None, side=None, source=None, limit=200):
        return _near(self, lat, lon, radius_m, state, side, source, limit)


# ---------------------------------------------------------------- VastDB

class VastDB:
    """VastDB backend (vastdb SDK on the data VIP). Tables live in schema `kerbmap`."""

    backend = "vastdb"

    def __init__(self, schema_name=None):
        import vastdb  # imported here so local mode works without the SDK

        cfg = load_vastdb_config()
        self.bucket_name = cfg["VASTDB_BUCKET"]
        self.schema_name = schema_name or os.environ.get("KERB_VDB_SCHEMA") or DEFAULT_VDB_SCHEMA
        if self.schema_name.startswith("vss"):
            raise RuntimeError("refusing to use a VSS schema for Kerb Map tables")
        self._session = vastdb.connect(endpoint=cfg["S3_ENDPOINT"], access=cfg["ACCESS_KEY"],
                                       secret=cfg["SECRET_KEY"], ssl_verify=False)

    def _schema(self, tx, create=False):
        bucket = tx.bucket(self.bucket_name)
        schema = bucket.schema(self.schema_name, fail_if_missing=False)
        if schema is None and create:
            schema = bucket.create_schema(self.schema_name, fail_if_exists=False)
        return schema

    def ensure_tables(self):
        with self._session.transaction() as tx:
            schema = self._schema(tx, create=True)
            for name, columns in TABLES.items():
                if schema.table(name, fail_if_missing=False) is None:
                    schema.create_table(name, columns=columns, fail_if_exists=False)

    def insert(self, table, rows):
        if not rows:
            return 0
        arrow = to_arrow(table, rows)
        with self._session.transaction() as tx:
            self._schema(tx).table(table).insert(arrow)
        return len(rows)

    @staticmethod
    def _predicate(t, filters):
        pred = None
        for col, value in (filters or {}).items():
            if isinstance(value, (list, tuple, set)):
                part = None
                for v in value:
                    p = t[col] == v
                    part = p if part is None else (part | p)
            elif value is None:
                part = t[col].isnull()
            else:
                part = t[col] == value
            if part is not None:
                pred = part if pred is None else (pred & part)
        return pred

    def _select(self, table, filters=None, extra=None, limit=None, row_id=False):
        with self._session.transaction() as tx:
            t = self._schema(tx).table(table)
            pred = self._predicate(t, filters)
            if extra is not None:
                more = extra(t)
                pred = more if pred is None else (pred & more)
            reader = t.select(columns=TABLES[table].names, predicate=pred,
                              internal_row_id=row_id, limit_rows=limit)
            return reader.read_all().to_pylist()

    def read(self, table, filters=None, limit=1000):
        if table not in TABLES:
            raise SchemaError(f"unknown table {table!r}")
        for col in filters or {}:
            if col not in TABLES[table].names:
                raise SchemaError(f"{table}: unknown filter column {col!r}")
        return self._select(table, filters, limit=int(limit))

    def _read_box(self, table, lat0, lat1, lon0, lon1, eq):
        def box(t):
            return (t["lat"] >= lat0) & (t["lat"] <= lat1) & (t["lon"] >= lon0) & (t["lon"] <= lon1)
        return self._select(table, eq, extra=box)

    def upsert_space(self, row):
        import pyarrow as pa

        validate_row("kerb_spaces", row)
        with self._session.transaction() as tx:
            t = self._schema(tx).table("kerb_spaces")
            found = t.select(columns=TABLES["kerb_spaces"].names,
                             predicate=(t["space_id"] == row["space_id"]),
                             internal_row_id=True).read_all().to_pylist()
            if not found:
                t.insert(to_arrow("kerb_spaces", [row]))
                return validate_row("kerb_spaces", row)
            current = found[0]
            row_id = current.pop("$row_id")
            merged = {**current, **row}
            arrow = to_arrow("kerb_spaces", [merged])
            cols = [c for c in row if c != "space_id"]
            if cols:
                update = arrow.select(cols).append_column("$row_id", pa.array([row_id], pa.uint64()))
                t.update(update, columns=cols)
            return merged

    def spaces_near(self, lat, lon, radius_m, state=None, side=None, source=None, limit=200):
        return _near(self, lat, lon, radius_m, state, side, source, limit)


# ---------------------------------------------------------------- factory

def get_db(backend=None):
    """Return a backend. `backend` or env KERB_DB: 'vastdb' or 'local'."""
    choice = (backend or os.environ.get("KERB_DB") or "").strip().lower()
    if not choice:
        choice = "vastdb" if _team_config_path() else "local"
    if choice == "local":
        return LocalDB()
    if choice == "vastdb":
        return VastDB()
    raise ValueError(f"KERB_DB must be 'vastdb' or 'local', got {choice!r}")
