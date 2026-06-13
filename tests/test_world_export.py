"""Extension B — world export: SDF + record sidecar + index, honesty-preserving."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Point

from plateau_bridge.schema import BUILDINGS_ARROW_SCHEMA
from plateau_bridge.world_export import export_world


def _row(uid: str, lat: float, lon: float, **over) -> dict:
    base: dict[str, object] = {name: None for name in BUILDINGS_ARROW_SCHEMA.names}
    base.update(
        building_uid=uid, gml_id=f"gml_{uid}", city_code="13113", dataset_year=2023,
        source_file_id="f1", geometry=Point(lon, lat).buffer(0.0001).wkb,
        centroid_lat=lat, centroid_lon=lon, height=9.0,
        source_url="https://example.jp/d", source_dataset_id="plateau_13113_2023",
        attribution="© Project PLATEAU / MLIT (CC BY 4.0)",
    )
    for kind in ("river_flood", "inland_flood", "tsunami", "storm_surge", "landslide"):
        base[f"{kind}_covered"] = False
    base.update(over)
    return base


@pytest.fixture()
def parquet(tmp_path: Path) -> str:
    rows = [
        _row("b1", 35.0, 139.0, river_flood_covered=True, river_flood_depth_max=2.5),
        _row("b2", 35.001, 139.001, river_flood_covered=False),  # uncovered
        _row("b3", 35.002, 139.002, tsunami_covered=True, tsunami_depth_max=1.0),
    ]
    cols = {name: [r[name] for r in rows] for name in BUILDINGS_ARROW_SCHEMA.names}
    path = tmp_path / "buildings.parquet"
    pq.write_table(pa.table(cols, schema=BUILDINGS_ARROW_SCHEMA), path)
    return str(path)


def test_export_writes_three_artifacts(parquet, tmp_path):
    res = export_world(parquet, tmp_path / "world")
    assert res.sdf_path.exists() and res.sidecar_path.exists() and res.index_path.exists()
    assert res.n_features == 3


def test_sdf_is_valid_xml_with_a_model_per_building(parquet, tmp_path):
    res = export_world(parquet, tmp_path / "world")
    root = ET.fromstring(res.sdf_path.read_bytes())
    world = root.find("world")
    models = world.findall("model")
    names = {m.get("name") for m in models}
    assert "ground_plane" in names
    bldg_models = [m for m in models if m.get("name", "").startswith("bldg_")]
    assert len(bldg_models) == 3
    # each building model has a box collision
    assert bldg_models[0].find("./link/collision/geometry/box/size") is not None


def test_sidecar_joins_to_record_layer_and_keeps_unknown_explicit(parquet, tmp_path):
    res = export_world(parquet, tmp_path / "world")
    tbl = pq.read_table(res.sidecar_path).to_pylist()
    assert len(tbl) == 3
    by_uid = {r["building_uid"]: r for r in tbl}
    assert set(by_uid) == {"b1", "b2", "b3"}
    # feature_id ↔ building_uid join key present and unique
    assert len({r["feature_id"] for r in tbl}) == 3

    # honesty carried into the sim: uncovered hazard keeps value null, not 0
    b2 = by_uid["b2"]
    assert b2["river_flood_covered"] is False
    assert b2["river_flood_value"] is None
    assert b2["unsurveyed_all"] is True   # no hazard covered → explicit unknown

    b1 = by_uid["b1"]
    assert b1["river_flood_covered"] is True
    assert b1["river_flood_value"] == 2.5
    assert b1["unsurveyed_all"] is False


def test_index_json_has_crs_origin_and_honesty_note(parquet, tmp_path):
    res = export_world(parquet, tmp_path / "world")
    idx = json.loads(res.index_path.read_text(encoding="utf-8"))
    assert idx["crs"] == "EPSG:6668"
    assert "lat" in idx["origin"] and "lon" in idx["origin"]
    assert idx["n_features"] == 3
    assert "unsurveyed" in idx["honesty_note"]
    assert "river_flood" in idx["coverage_layers"]


def test_empty_parquet_raises(tmp_path):
    cols = {name: [] for name in BUILDINGS_ARROW_SCHEMA.names}
    path = tmp_path / "empty.parquet"
    pq.write_table(pa.table(cols, schema=BUILDINGS_ARROW_SCHEMA), path)
    with pytest.raises(ValueError, match="no buildings"):
        export_world(str(path), tmp_path / "world")
