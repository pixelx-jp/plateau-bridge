"""DEM-derived inland (pluvial) flood susceptibility — offline pipeline + honesty."""

from __future__ import annotations

import io

import geopandas as gpd
import numpy as np
import pytest
from PIL import Image
from shapely.geometry import Point

from plateau_bridge.ops.pluvial_tci import (
    apply_inland_flood_susceptibility,
    compute_pluvial,
    pluvial_level,
)
from plateau_bridge.schema import (
    BUILDINGS_ARROW_SCHEMA,
    InlandFloodSusceptibilityField,
)


def _dem_png(elev: np.ndarray) -> bytes:
    x = np.round(elev / 0.01).astype(np.int64) % 16777216
    rgb = np.dstack([(x >> 16) & 255, (x >> 8) & 255, x & 255]).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG")
    return buf.getvalue()


# A plateau (10 m) with a deep square pit (4 m) in the middle → a closed
# depression that fills to the plateau level → ~6 m potential ponding in the pit.
_PIT = np.full((256, 256), 10.0, dtype=np.float32)
_PIT[100:156, 100:156] = 4.0
_PIT_PNG = _dem_png(_PIT)

# A monotonic ramp → no closed depression → ~0 ponding everywhere → level low.
_RAMP = np.repeat(np.linspace(0.0, 50.0, 256, dtype=np.float32)[:, None], 256, axis=1)
_RAMP_PNG = _dem_png(_RAMP)


def test_pluvial_level_thresholds():
    assert pluvial_level(0.0) == "low"
    assert pluvial_level(0.29) == "low"
    assert pluvial_level(0.3) == "medium"
    assert pluvial_level(0.99) == "medium"
    assert pluvial_level(1.0) == "high"
    assert pluvial_level(6.0) == "high"


def test_compute_pluvial_pit_ponds():
    pond, tci = compute_pluvial(_PIT.copy(), px=10.0, px_real=9.5)
    assert pond.shape == _PIT.shape
    # the pit fills to the plateau (~6 m); somewhere should pond deeply → high
    assert float(np.nanmax(pond)) > 1.0
    assert pluvial_level(float(np.nanmax(pond))) == "high"
    # the depression carries a finite TCI; well-drained cells are NaN
    assert np.isfinite(tci).any()


def test_compute_pluvial_ramp_is_dry():
    pond, _ = compute_pluvial(_RAMP.copy(), px=10.0, px_real=9.5)
    assert float(np.nanmax(pond)) < 0.05  # monotonic ramp → no closed depression


def test_compute_pluvial_empty():
    pond, tci = compute_pluvial(np.empty((0, 0), dtype=np.float32), px=10.0, px_real=9.5)
    assert pond.shape == (0, 0) and tci.shape == (0, 0)


def _gdf(coords):
    return gpd.GeoDataFrame(
        {"centroid_lon": [c[0] for c in coords], "centroid_lat": [c[1] for c in coords]},
        geometry=[Point(*c) for c in coords], crs=4326,
    )


def test_apply_disabled_leaves_uncovered():
    out = apply_inland_flood_susceptibility(_gdf([(139.76, 35.68)]), enabled=False)
    assert bool(out["inland_flood_susceptibility_covered"].iloc[0]) is False
    for c in ("inland_flood_susceptibility_level", "inland_flood_susceptibility_pond_m",
              "inland_flood_susceptibility_tci", "inland_flood_susceptibility_source_ids"):
        assert c in out.columns


def test_apply_enabled_samples_levels_offline():
    pts = [(139.7600, 35.6810), (139.7605, 35.6802), (139.7610, 35.6795)]
    out = apply_inland_flood_susceptibility(
        _gdf(pts), enabled=True, zoom=14, fetch=lambda url: _PIT_PNG
    )
    cov = out["inland_flood_susceptibility_covered"]
    assert cov.any()
    for i in range(len(out)):
        if bool(cov.iloc[i]):
            assert out["inland_flood_susceptibility_level"].iloc[i] in ("low", "medium", "high")
            assert np.isfinite(out["inland_flood_susceptibility_pond_m"].iloc[i])
            assert out["inland_flood_susceptibility_source_ids"].iloc[i] != ""
        else:
            assert out["inland_flood_susceptibility_level"].iloc[i] is None


def test_apply_missing_dem_is_uncovered():
    out = apply_inland_flood_susceptibility(
        _gdf([(139.76, 35.68)]), enabled=True, fetch=lambda url: None
    )
    assert bool(out["inland_flood_susceptibility_covered"].iloc[0]) is False
    assert out["inland_flood_susceptibility_level"].iloc[0] is None


def test_schema_and_field_honesty():
    names = set(BUILDINGS_ARROW_SCHEMA.names)
    for c in ("inland_flood_susceptibility_covered", "inland_flood_susceptibility_level",
              "inland_flood_susceptibility_pond_m", "inland_flood_susceptibility_tci",
              "inland_flood_susceptibility_source_ids",
              "inland_flood_susceptibility_coverage_confidence"):
        assert c in names
    with pytest.raises(ValueError):
        InlandFloodSusceptibilityField(covered=False, pond_m=2.0)
    ok = InlandFloodSusceptibilityField(covered=True, level="high", pond_m=1.4)
    assert ok.level == "high"
