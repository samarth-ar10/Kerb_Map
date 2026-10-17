import pytest

from common import kerbdb
from common.kerb_schema import TABLES, SchemaError, validate_row


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("KERB_DB", "local")
    monkeypatch.setenv("KERB_SQLITE", str(tmp_path / "kerb.sqlite"))
    d = kerbdb.get_db()
    d.ensure_tables()
    return d


def space(space_id, lat=43.6830, lon=-79.4205, state="free", side="L", source="dashcam", **kw):
    row = {"space_id": space_id, "source": source, "side": side, "state": state, "lat": lat, "lon": lon,
           "position_status": "ok", "confidence": 0.8, "receipt_text": "TEST: unit test row"}
    row.update(kw)
    return row


def test_six_tables_defined():
    assert set(TABLES) == {"kerb_spaces", "kerb_observations", "kerb_trail", "kerb_overrules",
                           "kerb_eval_cases", "kerb_cameras"}
    assert "embedding" in TABLES["kerb_spaces"].names


def test_get_db_local(db):
    assert db.backend == "local"


def test_unknown_field_rejected(db):
    with pytest.raises(SchemaError):
        db.insert("kerb_spaces", [space("s1", colour="red")])


def test_enum_checked(db):
    with pytest.raises(SchemaError):
        db.insert("kerb_spaces", [space("s1", state="parked")])
    with pytest.raises(SchemaError):
        validate_row("kerb_trail", {"trail_id": "t1", "step": "guess"})
    with pytest.raises(SchemaError):
        validate_row("kerb_cameras", {"camera_id": "c1", "geo_source": "gps"})


def test_missing_key_rejected(db):
    with pytest.raises(SchemaError):
        db.insert("kerb_observations", [{"space_id": "s1"}])


def test_bad_type_rejected(db):
    with pytest.raises(SchemaError):
        db.insert("kerb_spaces", [space("s1", lat="north")])


def test_insert_and_read_roundtrip(db):
    db.insert("kerb_spaces", [space("s1", embedding=[0.1, 0.2, 0.3]), space("s2", state="occupied")])
    rows = db.read("kerb_spaces", {"state": "free"}, limit=10)
    assert [r["space_id"] for r in rows] == ["s1"]
    assert rows[0]["embedding"] == pytest.approx([0.1, 0.2, 0.3])
    assert len(db.read("kerb_spaces", {"state": ["free", "occupied"]})) == 2
    assert len(db.read("kerb_spaces", None, limit=1)) == 1


def test_list_column_overrules(db):
    db.insert("kerb_overrules", [{"overrule_id": "o1", "space_id": "s1", "by": "tester", "new_state": "occupied",
                                  "note": "TEST", "at": "2026-10-17T12:00:00Z", "receipts_changed": ["s1", "s2"]}])
    assert db.read("kerb_overrules", {"space_id": "s1"})[0]["receipts_changed"] == ["s1", "s2"]


def test_upsert_space_merges(db):
    db.upsert_space(space("s1", street="Vaughan Road"))
    db.upsert_space({"space_id": "s1", "state": "occupied"})
    rows = db.read("kerb_spaces", {"space_id": "s1"})
    assert len(rows) == 1
    assert rows[0]["state"] == "occupied"
    assert rows[0]["street"] == "Vaughan Road"


def test_spaces_near_radius_and_filters(db):
    db.insert("kerb_spaces", [
        space("near_free", lat=43.6830, lon=-79.4205),
        space("near_occ", lat=43.6831, lon=-79.4206, state="occupied", side="R"),
        space("far", lat=43.7000, lon=-79.4205),
        space("no_pos", lat=None, lon=None, position_status="uncertain"),
    ])
    near = db.spaces_near(43.6830, -79.4205, radius_m=100)
    assert [r["space_id"] for r in near] == ["near_free", "near_occ"]
    assert near[0]["distance_m"] == 0.0
    assert [r["space_id"] for r in db.spaces_near(43.6830, -79.4205, 100, state="occupied")] == ["near_occ"]
    assert db.spaces_near(43.6830, -79.4205, 100, side="L", source="streetcam") == []


def test_spaces_near_uses_camera_position(db):
    db.insert("kerb_cameras", [{"camera_id": "neighborhood_cam-1", "kind": "street_camera", "lat": 43.6500,
                                "lon": -79.4000, "geo_source": "landmark_estimate", "geo_confidence": 0.5}])
    db.insert("kerb_spaces", [space("cam_space", lat=None, lon=None, source="streetcam", side="view",
                                    camera_id="neighborhood_cam-1", position_status="uncertain")])
    near = db.spaces_near(43.6501, -79.4000, radius_m=50)
    assert [r["space_id"] for r in near] == ["cam_space"]
    assert near[0]["position_from"] == "camera"


def test_haversine():
    assert kerbdb.haversine_m(43.0, -79.0, 43.0, -79.0) == 0.0
    assert 110000 < kerbdb.haversine_m(43.0, -79.0, 44.0, -79.0) < 112000
