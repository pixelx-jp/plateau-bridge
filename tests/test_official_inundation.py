"""Official inundation (GSI 浸水想定 raster) ingest — offline + don't-clobber."""

from __future__ import annotations

import io

import geopandas as gpd
import numpy as np
from PIL import Image
from shapely.geometry import Point

from plateau_bridge.ops.official_inundation import (
    _depth_for_color,
    apply_official_inundation,
)


def _rgba_png(rgb, alpha=255) -> bytes:
    a = np.zeros((256, 256, 4), dtype=np.uint8)
    a[:, :, 0], a[:, :, 1], a[:, :, 2] = rgb
    a[:, :, 3] = alpha
    buf = io.BytesIO()
    Image.fromarray(a, "RGBA").save(buf, format="PNG")
    return buf.getvalue()


_RED_5_10 = _rgba_png((255, 183, 183))      # legend 5–10 m → 7.5
_TRANSPARENT = _rgba_png((0, 0, 0), alpha=0)  # outside designated area


def _gdf(coords, **cols):
    g = gpd.GeoDataFrame(
        {"centroid_lon": [c[0] for c in coords], "centroid_lat": [c[1] for c in coords],
         **cols},
        geometry=[Point(*c) for c in coords], crs=4326,
    )
    return g


def test_depth_for_color_nearest_band():
    assert _depth_for_color(np.array([247, 245, 169])) == 0.25   # <0.5
    assert _depth_for_color(np.array([255, 183, 183])) == 7.5    # 5–10
    assert _depth_for_color(np.array([242, 133, 201])) == 20.0   # ≥20
    # a slightly-off colour snaps to the nearest band
    assert _depth_for_color(np.array([250, 180, 180])) == 7.5


def test_disabled_returns_unchanged():
    g = _gdf([(139.55, 35.31)])
    out = apply_official_inundation(g, "tsunami", enabled=False)
    assert "tsunami_covered" not in out.columns or out.equals(g)


def test_colored_tile_marks_covered_with_depth():
    pts = [(139.5500, 35.3100), (139.5505, 35.3102)]
    out = apply_official_inundation(_gdf(pts), "tsunami", enabled=True, fetch=lambda u: _RED_5_10)
    assert out["tsunami_covered"].all()
    assert (out["tsunami_depth_max"] == 7.5).all()
    assert (out["tsunami_coverage_confidence"] == "inundation_bounded").all()


def test_transparent_tile_leaves_uncovered():
    out = apply_official_inundation(_gdf([(139.55, 35.31)]), "tsunami", enabled=True,
                                    fetch=lambda u: _TRANSPARENT)
    assert bool(out["tsunami_covered"].iloc[0]) is False


def test_does_not_clobber_existing_plateau_coverage():
    # building already covered by PLATEAU with a depth → GSI must not overwrite it
    g = _gdf([(139.55, 35.31)], tsunami_covered=[True], tsunami_depth_max=[2.0])
    out = apply_official_inundation(g, "tsunami", enabled=True, fetch=lambda u: _RED_5_10)
    assert bool(out["tsunami_covered"].iloc[0]) is True
    assert float(out["tsunami_depth_max"].iloc[0]) == 2.0  # unchanged, not 7.5


def test_storm_surge_kind_also_supported():
    out = apply_official_inundation(_gdf([(139.8, 35.66)]), "storm_surge", enabled=True,
                                    fetch=lambda u: _RED_5_10)
    assert out["storm_surge_covered"].all()
    assert (out["storm_surge_depth_max"] == 7.5).all()
