from __future__ import annotations

from plateau_parquet.schema import (
    BUILDINGS_ARROW_SCHEMA,
    DEPTH_HAZARDS,
    HazardKind,
)


def test_arrow_schema_has_required_columns() -> None:
    names = set(BUILDINGS_ARROW_SCHEMA.names)
    for col in (
        "building_uid", "city_code", "dataset_year", "source_file_id",
        "geometry", "centroid_lat", "centroid_lon",
    ):
        assert col in names


def test_every_hazard_has_4_columns() -> None:
    names = set(BUILDINGS_ARROW_SCHEMA.names)
    for kind in HazardKind:
        prefix = kind.value
        # covered + coverage_source_ids + value + hit_source_ids + coverage_confidence
        cov = f"{prefix}_covered"
        src = f"{prefix}_coverage_source_ids"
        hit = f"{prefix}_hit_source_ids"
        conf = f"{prefix}_coverage_confidence"
        val = f"{prefix}_depth_max" if kind in DEPTH_HAZARDS else f"{prefix}_in_zone"
        for c in (cov, src, hit, conf, val):
            assert c in names, f"missing {c}"
