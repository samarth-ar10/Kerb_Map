"""Shared table contract for Kerb Map (PLAN.md section 3).

Six tables. Every writer (dashcam, street camera, query/overrule) uses these
schemas through common.kerbdb, which validates rows before writing.

Conventions:
- ids are strings; times (`last_seen`, `at`) are ISO-8601 UTC strings,
  e.g. "2026-10-17T12:40:00Z".
- `embedding` is a 256-float vector (Cosmos Embed1) or null.
- `detail_json` is a JSON-encoded string.
"""

import pyarrow as pa

SPACE_STATES = ("free", "occupied", "not_space", "cannot_tell")
SOURCES = ("dashcam", "streetcam")

TABLES = {
    "kerb_spaces": pa.schema([
        ("space_id", pa.utf8()),
        ("source", pa.utf8()),
        ("camera_id", pa.utf8()),
        ("original_video", pa.utf8()),
        ("segment_number", pa.int32()),
        ("t_sec", pa.float64()),
        ("side", pa.utf8()),
        ("state", pa.utf8()),
        ("reason", pa.utf8()),
        ("confidence", pa.float64()),
        ("lat", pa.float64()),
        ("lon", pa.float64()),
        ("position_status", pa.utf8()),
        ("street", pa.utf8()),
        ("length_m", pa.float64()),
        ("last_seen", pa.utf8()),
        ("evidence_uri", pa.utf8()),
        ("receipt_text", pa.utf8()),
        ("embedding", pa.list_(pa.float32())),
    ]),
    "kerb_observations": pa.schema([
        ("obs_id", pa.utf8()),
        ("space_id", pa.utf8()),
        ("source", pa.utf8()),
        ("camera_id", pa.utf8()),
        ("segment_source", pa.utf8()),
        ("t_sec", pa.float64()),
        ("state", pa.utf8()),
        ("confidence", pa.float64()),
    ]),
    "kerb_trail": pa.schema([
        ("trail_id", pa.utf8()),
        ("space_id", pa.utf8()),
        ("step_no", pa.int32()),
        ("step", pa.utf8()),
        ("detail_json", pa.utf8()),
        ("at", pa.utf8()),
    ]),
    "kerb_overrules": pa.schema([
        ("overrule_id", pa.utf8()),
        ("space_id", pa.utf8()),
        ("by", pa.utf8()),
        ("new_state", pa.utf8()),
        ("note", pa.utf8()),
        ("at", pa.utf8()),
        ("receipts_changed", pa.list_(pa.utf8())),
    ]),
    "kerb_eval_cases": pa.schema([
        ("case_id", pa.utf8()),
        ("source", pa.utf8()),
        ("original_video", pa.utf8()),
        ("t_sec", pa.float64()),
        ("x", pa.float64()),
        ("y", pa.float64()),
        ("label", pa.utf8()),
        ("labeller", pa.utf8()),
        ("split", pa.utf8()),
        ("frozen_sha256", pa.utf8()),
    ]),
    "kerb_cameras": pa.schema([
        ("camera_id", pa.utf8()),
        ("kind", pa.utf8()),
        ("lat", pa.float64()),
        ("lon", pa.float64()),
        ("heading", pa.float64()),
        ("place", pa.utf8()),
        ("geo_source", pa.utf8()),
        ("geo_confidence", pa.float64()),
        ("check_error_m", pa.float64()),
    ]),
}

# Primary key per table (must be present and non-empty on every row).
KEYS = {
    "kerb_spaces": "space_id",
    "kerb_observations": "obs_id",
    "kerb_trail": "trail_id",
    "kerb_overrules": "overrule_id",
    "kerb_eval_cases": "case_id",
    "kerb_cameras": "camera_id",
}

# Allowed values for enum-like columns. None (null) is always allowed.
ENUMS = {
    "kerb_spaces": {
        "source": SOURCES,
        "side": ("L", "R", "view"),
        "state": SPACE_STATES,
        "position_status": ("ok", "uncertain"),
    },
    "kerb_observations": {
        "source": SOURCES,
        "state": SPACE_STATES,
    },
    "kerb_trail": {
        "step": ("gate", "boxes", "geometry", "rule", "cosmos", "track", "position", "overrule"),
    },
    "kerb_overrules": {
        "new_state": SPACE_STATES,
    },
    "kerb_eval_cases": {
        "source": SOURCES,
        "split": ("dev", "heldout"),
    },
    "kerb_cameras": {
        "kind": ("dashcam", "street_camera"),
        "geo_source": ("pie_gps", "landmark_estimate", "refined", "unknown"),
    },
}


class SchemaError(ValueError):
    """A row does not match the table contract."""


def validate_row(table: str, row: dict) -> dict:
    """Check one row against the contract and return it with missing columns set to None.

    Rejects unknown tables, unknown fields, a missing key and enum values outside the allowed set.
    Type problems are caught when the row is converted to Arrow (see to_arrow).
    """
    if table not in TABLES:
        raise SchemaError(f"unknown table {table!r}")
    schema = TABLES[table]
    names = schema.names
    unknown = set(row) - set(names)
    if unknown:
        raise SchemaError(f"{table}: unknown field(s) {sorted(unknown)}")
    key = KEYS[table]
    if not row.get(key):
        raise SchemaError(f"{table}: missing key field {key!r}")
    for col, allowed in ENUMS.get(table, {}).items():
        value = row.get(col)
        if value is not None and value not in allowed:
            raise SchemaError(f"{table}.{col}: {value!r} not in {list(allowed)}")
    return {name: row.get(name) for name in names}


def to_arrow(table: str, rows: list) -> pa.Table:
    """Validate rows and convert them to an Arrow table with the contract schema."""
    clean = [validate_row(table, r) for r in rows]
    try:
        return pa.Table.from_pylist(clean, schema=TABLES[table])
    except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError) as exc:
        raise SchemaError(f"{table}: bad value type: {exc}") from exc
