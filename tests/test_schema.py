from __future__ import annotations

import pytest

from plateau_bridge.schema import (
    BUILDINGS_ARROW_SCHEMA,
    DEPTH_HAZARDS,
    EarthquakeField,
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


def test_seismic_columns_present() -> None:
    names = set(BUILDINGS_ARROW_SCHEMA.names)
    for c in (
        "earthquake_covered",
        "earthquake_prob_strong_shaking_30yr",
        "earthquake_amplification",
        "earthquake_meshcode",
        "earthquake_source_ids",
        "earthquake_coverage_confidence",
    ):
        assert c in names, f"missing {c}"


def test_earthquake_field_honesty_covered_false_requires_null() -> None:
    # covered=false with a probability is a lie (missing ≠ 0/safe) → rejected.
    with pytest.raises(ValueError):
        EarthquakeField(covered=False, prob_strong_shaking_30yr=0.06)
    # explicit unknown is fine.
    f = EarthquakeField(covered=False)
    assert f.prob_strong_shaking_30yr is None and f.amplification is None
    # covered with a valid probability is fine.
    ok = EarthquakeField(covered=True, prob_strong_shaking_30yr=0.062, amplification=1.8)
    assert ok.prob_strong_shaking_30yr == 0.062
    # out-of-range probability rejected.
    with pytest.raises(ValueError):
        EarthquakeField(covered=True, prob_strong_shaking_30yr=1.5)
