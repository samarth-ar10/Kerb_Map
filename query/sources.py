"""Read the builders' JSONL fallback files so the app works before rows reach the database.

Files: <repo>/dashcam/out/*.jsonl and <repo>/streetcam/out/*.jsonl (override with KERB_JSONL_DIRS,
a list separated by os.pathsep). Which table a line belongs to:
  1. a "table" field on the line ({"table": "kerb_spaces", "space_id": ..., ...}), else
  2. the file name. The table name is the whole stem, a prefix (kerb_trail_set01.jsonl),
     or a suffix (set01_kerb_spaces.jsonl). The longest table name wins.
Lines are validated against common.kerb_schema; bad lines are skipped and counted, never fixed up.
"""

import glob
import json
import os

from common.kerb_schema import KEYS, TABLES, SchemaError, validate_row

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def jsonl_dirs():
    env = os.environ.get("KERB_JSONL_DIRS")
    if env:
        return [d for d in env.split(os.pathsep) if d]
    return [os.path.join(REPO, "dashcam", "out"), os.path.join(REPO, "streetcam", "out")]


def _table_from_name(path):
    base = os.path.basename(path)
    stem = base[:-6] if base.endswith(".jsonl") else base
    for name in sorted(TABLES, key=len, reverse=True):
        if stem == name or stem.startswith(name + "_") or stem.endswith("_" + name):
            return name
    return None


class JsonlSource:
    """In-memory rows from JSONL files, reloaded when a file changes. Read-only."""

    backend = "jsonl"

    def __init__(self, dirs=None):
        self.dirs = dirs or jsonl_dirs()
        self._stamp = None
        self.rows = {t: {} for t in TABLES}
        self.skipped = 0
        self.files = []

    def _files(self):
        out = []
        for d in self.dirs:
            out.extend(sorted(glob.glob(os.path.join(d, "*.jsonl"))))
        return out

    def refresh(self):
        files = self._files()
        stamp = tuple((f, os.path.getmtime(f), os.path.getsize(f)) for f in files)
        if stamp == self._stamp:
            return
        rows = {t: {} for t in TABLES}
        skipped = 0
        for path in files:
            default_table = _table_from_name(path)
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        table = obj.pop("table", None) or default_table
                        if table not in TABLES:
                            raise SchemaError("no table")
                        row = validate_row(table, obj)
                    except (ValueError, SchemaError):
                        skipped += 1
                        continue
                    rows[table][row[KEYS[table]]] = row  # later lines win
        self.rows, self.skipped, self.files, self._stamp = rows, skipped, files, stamp

    def read(self, table, filters=None, limit=1000):
        self.refresh()
        out = []
        for row in self.rows[table].values():
            ok = True
            for col, value in (filters or {}).items():
                if isinstance(value, (list, tuple, set)):
                    ok = row.get(col) in value
                else:
                    ok = row.get(col) == value
                if not ok:
                    break
            if ok:
                out.append(row)
                if len(out) >= limit:
                    break
        return out

    def _read_box(self, table, lat0, lat1, lon0, lon1, eq):
        return [r for r in self.read(table, eq, limit=10 ** 9)
                if r.get("lat") is not None and r.get("lon") is not None
                and lat0 <= r["lat"] <= lat1 and lon0 <= r["lon"] <= lon1]

    def spaces_near(self, lat, lon, radius_m, state=None, side=None, source=None, limit=200):
        from common.kerbdb import _near
        return _near(self, lat, lon, radius_m, state, side, source, limit)
