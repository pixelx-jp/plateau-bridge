"""DEM-derived landslide susceptibility (slope) — offline pipeline + honesty."""

from __future__ import annotations

import io

import geopandas as gpd
import numpy as np
import pytest
from PIL import Image
from shapely.geometry import Point

from plateau_bridge.ops.slope import (
    apply_landslide_susceptibility,
    compute_slope_deg,
    slope_level,
)
from plateau_bridge.schema import (
    BUILDINGS_ARROW_SCHEMA,
    LandslideSusceptibilityField,
)


def _dem_png(elev: np.ndarray) -> bytes:
    """Encode a 256x256 elevation array (m) into GSI dem_png RGB bytes."""
    x = np.round(elev / 0.01).astype(np.int64) % 16777216
    rgb = np.dstack([(x >> 16) & 255, (x >> 8) & 255, x & 255]).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG")
    return buf.getvalue()


# A synthetic tile that climbs steeply from top to bottom: 0→200 m over 256 px.
# At zoom 14 (~9.5 m/px) that's ~0.8 m rise per px → a meaningful slope so the
# sampler returns a finite, non-trivial angle everywhere.
_rows = np.linspace(0.0, 200.0, 256, dtype=np.float32)
_STEEP = np.repeat(_rows[:, None], 256, axis=1)
_STEEP_PNG = _dem_png(_STEEP)

# A perfectly flat tile → slope 0° everywhere → level "low".
_FLAT = np.full((256, 256), 5.0, dtype=np.float32)
_FLAT_PNG = _dem_png(_FLAT)


def test_slope_level_thresholds():
    assert slope_level(0.0) == "low"
    assert slope_level(4.9) == "low"
    assert slope_level(5.0) == "medium"
    assert slope_level(14.9) == "medium"
    assert slope_level(15.0) == "high"
    assert slope_level(45.0) == "high"


def test_compute_slope_flat_is_zero():
    slope = compute_slope_deg(_FLAT.copy(), px_real=9.5)
    finite = slope[np.isfinite(slope)]
    assert finite.size > 0
    assert float(np.nanmax(slope)) < 0.01  # flat terrain → ~0°


def test_compute_slope_ramp_is_positive_and_consistent():
    px_real = 9.5
    slope = compute_slope_deg(_STEEP.copy(), px_real=px_real)
    interior = slope[1:-1, 1:-1]  # ignore gradient edge effects
    # rise per px = 200/255 m over px_real m → expected angle
    expected = np.degrees(np.arctan((200.0 / 255.0) / px_real))
    assert abs(float(np.median(interior)) - expected) < 0.5


def test_compute_slope_empty_is_nan_grid():
    out = compute_slope_deg(np.empty((0, 0), dtype=np.float32), px_real=9.5)
    assert out.shape == (0, 0)


def _gdf(coords):  # (lon, lat)
    return gpd.GeoDataFrame(
        {"centroid_lon": [c[0] for c in coords], "centroid_lat": [c[1] for c in coords]},
        geometry=[Point(*c) for c in coords], crs=4326,
    )


def test_apply_disabled_leaves_uncovered():
    out = apply_landslide_susceptibility(_gdf([(139.76, 35.68)]), enabled=False)
    assert bool(out["landslide_susceptibility_covered"].iloc[0]) is False
    for c in ("landslide_susceptibility_level", "landslide_susceptibility_slope_deg",
              "landslide_susceptibility_source_ids"):
        assert c in out.columns


def test_apply_enabled_samples_levels_offline():
    pts = [(139.7600, 35.6810), (139.7605, 35.6802), (139.7610, 35.6795)]
    out = apply_landslide_susceptibility(
        _gdf(pts), enabled=True, zoom=14, fetch=lambda url: _STEEP_PNG
    )
    cov = out["landslide_susceptibility_covered"]
    assert cov.any(), "expected some buildings to get a slope value"
    for i in range(len(out)):
        if bool(cov.iloc[i]):
            assert out["landslide_susceptibility_level"].iloc[i] in ("low", "medium", "high")
            assert np.isfinite(out["landslide_susceptibility_slope_deg"].iloc[i])
            assert out["landslide_susceptibility_source_ids"].iloc[i] != ""
        else:
            assert out["landslide_susceptibility_level"].iloc[i] is None


def test_apply_flat_terrain_is_low():
    out = apply_landslide_susceptibility(
        _gdf([(139.7600, 35.6810)]), enabled=True, zoom=14, fetch=lambda url: _FLAT_PNG
    )
    if bool(out["landslide_susceptibility_covered"].iloc[0]):
        assert out["landslide_susceptibility_level"].iloc[0] == "low"


def test_apply_missing_dem_is_uncovered():
    out = apply_landslide_susceptibility(
        _gdf([(139.76, 35.68)]), enabled=True, fetch=lambda url: None  # no DEM tiles
    )
    assert bool(out["landslide_susceptibility_covered"].iloc[0]) is False
    assert out["landslide_susceptibility_level"].iloc[0] is None


def test_schema_and_field_honesty():
    names = set(BUILDINGS_ARROW_SCHEMA.names)
    for c in ("landslide_susceptibility_covered", "landslide_susceptibility_level",
              "landslide_susceptibility_slope_deg", "landslide_susceptibility_source_ids",
              "landslide_susceptibility_coverage_confidence"):
        assert c in names
    with pytest.raises(ValueError):
        LandslideSusceptibilityField(covered=False, slope_deg=20.0)  # covered=false must be null
    ok = LandslideSusceptibilityField(covered=True, level="high", slope_deg=22.0)
    assert ok.level == "high"
