"""DEM-derived flood susceptibility (HAND) — offline pipeline + honesty."""

from __future__ import annotations

import io

import geopandas as gpd
import numpy as np
import pytest
from PIL import Image
from shapely.geometry import Point

from plateau_bridge.ops.hand import (
    apply_flood_susceptibility,
    compute_hand,
    hand_level,
    lonlat_to_3857,
)
from plateau_bridge.schema import (
    BUILDINGS_ARROW_SCHEMA,
    FloodSusceptibilityField,
)


def _dem_png(elev: np.ndarray) -> bytes:
    """Encode a 256x256 elevation array (m) into GSI dem_png RGB bytes."""
    x = np.round(elev / 0.01).astype(np.int64) % 16777216
    rgb = np.dstack([(x >> 16) & 255, (x >> 8) & 255, x & 255]).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG")
    return buf.getvalue()


# A synthetic tile: elevation decreases toward the bottom rows → flow drains
# downward, the bottom becomes the drainage line; HAND grows with height above it.
_rows = np.linspace(50.0, 0.0, 256, dtype=np.float32)
_SLOPE = np.repeat(_rows[:, None], 256, axis=1)
_TILE_PNG = _dem_png(_SLOPE)


def test_hand_level_thresholds():
    assert hand_level(0.0) == "high"
    assert hand_level(3.0) == "high"
    assert hand_level(5.0) == "medium"
    assert hand_level(10.0) == "medium"
    assert hand_level(20.0) == "low"


def test_compute_hand_runs_and_is_nonneg():
    hand = compute_hand(_SLOPE.copy(), px=10.0, acc_cells=50)
    finite = hand[np.isfinite(hand)]
    assert finite.size > 0
    assert float(finite.min()) >= -1e-3  # HAND is height above drainage → ~>=0


def _gdf(coords):  # (lon, lat)
    return gpd.GeoDataFrame(
        {"centroid_lon": [c[0] for c in coords], "centroid_lat": [c[1] for c in coords]},
        geometry=[Point(*c) for c in coords], crs=4326,
    )


def test_apply_disabled_leaves_uncovered():
    out = apply_flood_susceptibility(_gdf([(139.76, 35.68)]), enabled=False)
    assert bool(out["flood_susceptibility_covered"].iloc[0]) is False
    for c in ("flood_susceptibility_level", "flood_susceptibility_hand_m",
              "flood_susceptibility_source_ids"):
        assert c in out.columns


def test_apply_enabled_samples_levels_offline():
    # tight bbox → single tile; fake fetch returns the synthetic slope tile.
    pts = [(139.7600, 35.6810), (139.7605, 35.6802), (139.7610, 35.6795)]
    out = apply_flood_susceptibility(
        _gdf(pts), enabled=True, zoom=14, acc_cells=50, fetch=lambda url: _TILE_PNG
    )
    cov = out["flood_susceptibility_covered"]
    assert cov.any(), "expected some buildings to get a HAND value"
    for i in range(len(out)):
        if bool(cov.iloc[i]):
            assert out["flood_susceptibility_level"].iloc[i] in ("low", "medium", "high")
            assert np.isfinite(out["flood_susceptibility_hand_m"].iloc[i])
            assert out["flood_susceptibility_source_ids"].iloc[i] != ""
        else:
            assert out["flood_susceptibility_level"].iloc[i] is None


def test_apply_missing_dem_is_uncovered():
    out = apply_flood_susceptibility(
        _gdf([(139.76, 35.68)]), enabled=True, fetch=lambda url: None  # no DEM tiles
    )
    assert bool(out["flood_susceptibility_covered"].iloc[0]) is False
    assert out["flood_susceptibility_level"].iloc[0] is None


def test_schema_and_field_honesty():
    names = set(BUILDINGS_ARROW_SCHEMA.names)
    for c in ("flood_susceptibility_covered", "flood_susceptibility_level",
              "flood_susceptibility_hand_m", "flood_susceptibility_source_ids",
              "flood_susceptibility_coverage_confidence"):
        assert c in names
    with pytest.raises(ValueError):
        FloodSusceptibilityField(covered=False, hand_m=2.0)  # covered=false must be null
    ok = FloodSusceptibilityField(covered=True, level="high", hand_m=1.2)
    assert ok.level == "high"


def test_lonlat_to_3857_roundtrip_sane():
    x, y = lonlat_to_3857(139.7671, 35.6812)
    assert 15.5e6 < x < 15.6e6 and 4.2e6 < y < 4.3e6  # Tokyo in Web Mercator
