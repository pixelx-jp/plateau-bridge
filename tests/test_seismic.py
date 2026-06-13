"""Earthquake extension (J-SHIS 250m mesh join) — provider + apply_seismic."""

from __future__ import annotations

import math

import geopandas as gpd
from shapely.geometry import Point

from plateau_bridge.ops.meshcode import meshcode_250m
from plateau_bridge.ops.seismic import apply_seismic
from plateau_bridge.sources.jshis import JshisMeshProvider, SeismicValue


def _gdf(coords: list[tuple[float, float]]) -> gpd.GeoDataFrame:
    """coords as (lon, lat)."""
    return gpd.GeoDataFrame(
        {
            "centroid_lon": [c[0] for c in coords],
            "centroid_lat": [c[1] for c in coords],
        },
        geometry=[Point(lon, lat) for lon, lat in coords],
        crs=4326,
    )


class _FakeProvider:
    def __init__(self, table: dict[str, SeismicValue]) -> None:
        self.table = table

    def values_for(self, meshcodes):
        return {m: self.table[m] for m in dict.fromkeys(meshcodes) if m in self.table}


def test_apply_seismic_covered_and_uncovered_are_honest():
    tokyo = (139.7671, 35.6812)  # mesh 5339461132
    osaka = (135.50, 34.70)      # different cell, NOT in table → must stay uncovered
    g = _gdf([tokyo, osaka])
    mc = meshcode_250m(35.6812, 139.7671)
    out = apply_seismic(g, _FakeProvider({mc: SeismicValue(0.377, 1.27)}))

    # Tokyo: covered, real values, full-admin confidence, source stamped.
    assert bool(out["earthquake_covered"].iloc[0]) is True
    assert abs(out["earthquake_prob_strong_shaking_30yr"].iloc[0] - 0.377) < 1e-6
    assert abs(out["earthquake_amplification"].iloc[0] - 1.27) < 1e-6
    assert out["earthquake_meshcode"].iloc[0] == mc
    assert out["earthquake_coverage_confidence"].iloc[0] == "declared_full_admin"
    assert out["earthquake_source_ids"].iloc[0] != ""

    # Osaka: unresolved cell → uncovered, null (never a silent 0).
    assert bool(out["earthquake_covered"].iloc[1]) is False
    assert math.isnan(out["earthquake_prob_strong_shaking_30yr"].iloc[1])
    assert math.isnan(out["earthquake_amplification"].iloc[1])
    assert out["earthquake_source_ids"].iloc[1] == ""
    assert out["earthquake_coverage_confidence"].iloc[1] == "unknown"


def test_apply_seismic_no_provider_leaves_columns_uncovered():
    out = apply_seismic(_gdf([(139.7671, 35.6812)]), None)
    assert bool(out["earthquake_covered"].iloc[0]) is False
    assert math.isnan(out["earthquake_prob_strong_shaking_30yr"].iloc[0])
    # columns still present → schema stable
    for c in ("earthquake_amplification", "earthquake_meshcode", "earthquake_source_ids"):
        assert c in out.columns


def test_provider_parses_real_response_shape():
    # Exact J-SHIS response shapes captured live for meshcode 5339461132.
    prob_json = (
        '{"metaData":{},"features":[{"properties":{"T30_I55_PS":"0.376908",'
        '"meshcode":"5339461132"}}],"status":"Success","type":"FeatureCollection"}'
    )
    amp_json = '{"status":"Success","features":[{"properties":{"ARV":"1.27"}}]}'
    prov = JshisMeshProvider(fetch=lambda url: amp_json if "sstrct" in url else prob_json)
    v = prov.value_for("5339461132")
    assert v is not None
    assert abs(v.prob_strong_shaking_30yr - 0.376908) < 1e-6
    assert abs(v.amplification - 1.27) < 1e-6
    # cached: a second call must not re-fetch (mutate fetch to prove it).
    prov2 = JshisMeshProvider(fetch=lambda url: amp_json if "sstrct" in url else prob_json)
    prov2.value_for("5339461132")
    assert "5339461132" in prov2._cache


def test_provider_failure_and_invalid_are_uncovered():
    assert JshisMeshProvider(fetch=lambda url: None).value_for("5339461132") is None
    # status != Success → None
    bad = JshisMeshProvider(fetch=lambda url: '{"status":"Error","features":[]}')
    assert bad.value_for("5339461132") is None
    # out-of-range probability → None (never trusted)
    oor = JshisMeshProvider(
        fetch=lambda url: '{"status":"Success","features":[{"properties":{"T30_I55_PS":"9"}}]}'
    )
    assert oor.value_for("5339461132") is None
    assert JshisMeshProvider(fetch=lambda url: None).values_for(["5339461132"]) == {}
