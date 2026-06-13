"""Shared query+honesty core — incl. the read-side 'no data ⇒ never safe' test."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Point

from plateau_bridge.query import HAZARD_ATTRS, RecordQuery, UnknownAssetError
from plateau_bridge.schema import BUILDINGS_ARROW_SCHEMA


def _row(uid: str, lat: float, lon: float, **over) -> dict:
    base: dict[str, object] = {name: None for name in BUILDINGS_ARROW_SCHEMA.names}
    base.update(
        building_uid=uid,
        gml_id=f"gml_{uid}",
        city_code="13113",
        dataset_year=2023,
        source_file_id="f1",
        geometry=Point(lon, lat).wkb,
        centroid_lat=lat,
        centroid_lon=lon,
        source_url="https://example.jp/d",
        source_dataset_id="plateau_13113_2023",
        attribution="© Project PLATEAU / MLIT (CC BY 4.0)",
    )
    # default every hazard to uncovered/unknown
    for kind in ("river_flood", "inland_flood", "tsunami", "storm_surge", "landslide"):
        base[f"{kind}_covered"] = False
    base.update(over)
    return base


@pytest.fixture()
def parquet(tmp_path: Path) -> str:
    rows = [
        _row("b1", 35.0, 139.0,
             river_flood_covered=True, river_flood_depth_max=2.5,
             river_flood_coverage_confidence="explicit_polygon",
             landslide_covered=True, landslide_in_zone=True,
             condition_covered=True, condition_state="危険",
             condition_confidence=0.7, condition_confidence_tier="inferred",
             condition_source_ids="triage_noto",
             condition_observed_at="2024-01-05T00:00:00Z"),
        _row("b2", 35.0002, 139.0002,
             river_flood_covered=False,          # <- the uncovered one
             tsunami_covered=True, tsunami_depth_max=None),  # assessed, no depth
        _row("b4", 36.0, 140.0),                 # far away, all unknown
    ]
    cols = {name: [r[name] for r in rows] for name in BUILDINGS_ARROW_SCHEMA.names}
    table = pa.table(cols, schema=BUILDINGS_ARROW_SCHEMA)
    path = tmp_path / "buildings.parquet"
    pq.write_table(table, path)
    return str(path)


def test_covered_record_carries_value(parquet):
    with RecordQuery(parquet) as q:
        r = q.get_record("b1", "river_flood_depth_max")
        assert r.covered is True
        assert r.value == 2.5 and r.unit == "m"
        assert r.confidence_tier == "modelled"
        assert "explicit_polygon" in r.source.method


def test_uncovered_is_unknown_never_safe(parquet):
    """The core guarantee: a building outside coverage is unknown, not depth 0."""
    with RecordQuery(parquet) as q:
        r = q.get_record("b2", "river_flood_depth_max")
        assert r.covered is False
        assert r.value is None            # not 0, not "safe"
        assert r.confidence is None


def test_no_record_ever_has_covered_false_with_value(parquet):
    """Sweep every asset × attribute: the invariant must hold universally."""
    with RecordQuery(parquet) as q:
        for uid in ("b1", "b2", "b4"):
            for rec in q.query_asset(uid):
                if not rec.covered:
                    assert rec.value is None, (uid, rec.attribute, rec.value)


def test_covered_true_no_depth_is_assessed_not_unknown(parquet):
    with RecordQuery(parquet) as q:
        r = q.get_record("b2", "tsunami_depth_max")
        assert r.covered is True          # assessed...
        assert r.value is None            # ...and found no depth (distinct from unknown)


def test_landslide_in_zone_bool(parquet):
    with RecordQuery(parquet) as q:
        r = q.get_record("b1", "landslide_in_zone")
        assert r.covered is True and r.value is True


def test_query_point_radius(parquet):
    with RecordQuery(parquet) as q:
        recs = q.query_point(35.0, 139.0, radius_m=100, attributes=["river_flood_depth_max"])
        uids = {r.asset_id for r in recs}
        assert "b1" in uids and "b2" in uids   # within ~30m
        assert "b4" not in uids                # ~140km away


def test_coverage_rollup(parquet):
    with RecordQuery(parquet) as q:
        cov = q.coverage()
        assert cov["total"] == 3
        rf = cov["by_attribute"]["river_flood_depth_max"]
        assert rf["covered"] == 1 and rf["unknown"] == 2


def test_cite_provenance(parquet):
    with RecordQuery(parquet) as q:
        r = q.get_record("b1", "river_flood_depth_max")
        chain = q.cite(r)
        assert chain["source"]["dataset_id"] == "plateau_13113_2023"
        assert chain["attribute"] == "river_flood_depth_max"


def test_errors(parquet):
    with RecordQuery(parquet) as q:
        with pytest.raises(ValueError):
            q.get_record("b1", "not_a_real_attribute")
        with pytest.raises(UnknownAssetError):
            q.get_record("nope", "river_flood_depth_max")


def test_attribute_registry_complete():
    assert "river_flood_depth_max" in HAZARD_ATTRS
    assert "landslide_in_zone" in HAZARD_ATTRS


# -- extension A: condition (damage_state) through the same core ----------

def test_condition_attribute_available(parquet):
    with RecordQuery(parquet) as q:
        assert "damage_state" in q.attributes


def test_condition_covered_record(parquet):
    with RecordQuery(parquet) as q:
        r = q.get_record("b1", "damage_state")
        assert r.covered is True
        assert r.value == "危険"
        assert r.confidence_tier == "inferred"
        assert r.confidence == pytest.approx(0.7, abs=1e-6)


def test_condition_uncovered_is_unknown(parquet):
    with RecordQuery(parquet) as q:
        r = q.get_record("b2", "damage_state")  # b2 has no condition data
        assert r.covered is False and r.value is None
