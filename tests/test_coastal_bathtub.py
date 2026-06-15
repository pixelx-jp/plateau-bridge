"""Connectivity bathtub (津波/高潮 terrain susceptibility) — offline + honesty."""

from __future__ import annotations

import io

import geopandas as gpd
import numpy as np
from PIL import Image
from shapely.geometry import Point

from plateau_bridge.ops.coastal_bathtub import (
    apply_coastal_bathtub,
    bathtub_level,
    compute_bathtub,
)
from plateau_bridge.schema import BUILDINGS_ARROW_SCHEMA


def _dem_png(elev: np.ndarray) -> bytes:
    """Encode elevation; NaN → GSI nodata sentinel RGB(128,0,0) (= ocean)."""
    nan = ~np.isfinite(elev)
    x = np.round(np.nan_to_num(elev) / 0.01).astype(np.int64) % 16777216
    rgb = np.dstack([(x >> 16) & 255, (x >> 8) & 255, x & 255]).astype(np.uint8)
    rgb[nan] = (128, 0, 0)  # GSI no-data = ocean
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG")
    return buf.getvalue()


# A coast: leftmost columns are SEA (nodata), rising inland to the right (land,
# incl. a real ≤0 m strip near the coast that must stay LAND/flood-receiving).
# A disconnected inland pit (deep) must NOT flood (connectivity test).
_COAST = np.tile(np.linspace(-1.0, 12.0, 256, dtype=np.float32), (256, 1))
_COAST[:, :20] = np.nan          # ocean (nodata) on the left border
_COAST[120:130, 200:210] = -5.0  # inland pit below sea level but NOT coast-connected
_COAST_PNG = _dem_png(_COAST)


def test_bathtub_level_thresholds():
    assert bathtub_level(0.1) == "low"
    assert bathtub_level(0.5) == "medium"
    assert bathtub_level(2.9) == "medium"
    assert bathtub_level(3.0) == "high"


def test_compute_bathtub_floods_connected_low_not_disconnected_pit():
    depth = compute_bathtub(_COAST.copy(), seed_m=3.0, px_real=10.0)
    # coast-connected low land floods
    assert np.isfinite(depth).any()
    assert float(np.nanmax(depth)) > 0
    # the disconnected inland pit must stay dry (NaN) — connectivity, not naive bathtub
    assert not np.isfinite(depth[124, 205])


def test_compute_bathtub_zero_m_land_is_flood_receiving_not_sea():
    # a real ≤0 m coast strip (NOT nodata) must be treated as LAND that floods,
    # never as sea/source (the P0 fix). Column 25 is land at ≈ -0.7 m, coast-adjacent.
    depth = compute_bathtub(_COAST.copy(), seed_m=3.0, px_real=10.0)
    assert np.isfinite(depth[128, 25])          # 0m-zone land IS flagged (flooded)
    assert float(depth[128, 25]) > 0


def test_compute_bathtub_attenuation_decreases_inland():
    # flat low plain behind a nodata-sea border → depth must DECREASE with distance.
    flat = np.zeros((64, 400), dtype=np.float32)
    flat[:, :10] = np.nan  # sea
    d = compute_bathtub(flat, seed_m=5.0, px_real=50.0, attenuation=0.0002)
    near = float(d[32, 20])
    far = float(d[32, 380])
    assert np.isfinite(near) and near > far  # attenuated inland


def test_compute_bathtub_no_sea_no_flood():
    # all-high terrain, NO nodata → no ocean seed → nothing floods
    hi = np.full((128, 128), 30.0, dtype=np.float32)
    depth = compute_bathtub(hi, seed_m=5.0, px_real=10.0)
    assert not np.isfinite(depth).any()


def test_compute_bathtub_low_land_no_nodata_no_flood():
    # a basin of real ≤0 m land with NO nodata anywhere → no sea → must NOT flood
    # (proves we don't treat ≤0 m as sea, and don't flood without an ocean seed).
    low = np.full((64, 64), -2.0, dtype=np.float32)
    depth = compute_bathtub(low, seed_m=5.0, px_real=10.0)
    assert not np.isfinite(depth).any()


def _gdf(coords):
    return gpd.GeoDataFrame(
        {"centroid_lon": [c[0] for c in coords], "centroid_lat": [c[1] for c in coords]},
        geometry=[Point(*c) for c in coords], crs=4326,
    )


def test_apply_disabled_leaves_uncovered():
    out = apply_coastal_bathtub(_gdf([(139.8, 35.6)]), "tsunami", seed_m=3.0, enabled=False)
    assert bool(out["tsunami_susceptibility_covered"].iloc[0]) is False
    for c in ("tsunami_susceptibility_level", "tsunami_susceptibility_depth_m",
              "tsunami_susceptibility_source_ids"):
        assert c in out.columns


def test_apply_zero_seed_is_noop():
    out = apply_coastal_bathtub(_gdf([(139.8, 35.6)]), "storm_surge", seed_m=0.0, enabled=True,
                                fetch=lambda u: _COAST_PNG)
    assert bool(out["storm_surge_susceptibility_covered"].iloc[0]) is False


def test_apply_offline_samples_levels():
    pts = [(139.800, 35.600), (139.805, 35.602)]
    out = apply_coastal_bathtub(_gdf(pts), "tsunami", seed_m=3.0, enabled=True,
                                fetch=lambda u: _COAST_PNG)
    for i in range(len(out)):
        if bool(out["tsunami_susceptibility_covered"].iloc[i]):
            assert out["tsunami_susceptibility_level"].iloc[i] in ("low", "medium", "high")
            assert np.isfinite(out["tsunami_susceptibility_depth_m"].iloc[i])
        else:
            assert out["tsunami_susceptibility_level"].iloc[i] is None


def test_schema_has_columns():
    names = set(BUILDINGS_ARROW_SCHEMA.names)
    for kind in ("tsunami", "storm_surge"):
        for suf in ("covered", "level", "depth_m", "source_ids", "coverage_confidence"):
            assert f"{kind}_susceptibility_{suf}" in names
