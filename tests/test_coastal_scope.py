"""対象外 (terrain-certain non-exposure) classification — offline + honesty."""

from __future__ import annotations

import io

import geopandas as gpd
import numpy as np
from PIL import Image
from shapely.geometry import Point

from plateau_bridge.ops.coastal_scope import apply_coastal_scope
from plateau_bridge.schema import BUILDINGS_ARROW_SCHEMA


def _dem_png(elev_m: float) -> bytes:
    x = int(round(elev_m / 0.01)) % 16777216
    rgb = np.zeros((256, 256, 3), dtype=np.uint8)
    rgb[:, :, 0] = (x >> 16) & 255
    rgb[:, :, 1] = (x >> 8) & 255
    rgb[:, :, 2] = x & 255
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG")
    return buf.getvalue()


_HIGH = _dem_png(35.0)   # 武蔵野台地-ish, above tsunami/surge reach
_LOW = _dem_png(2.0)     # lowland, below reach


def _gdf(coords, covered=None):
    g = gpd.GeoDataFrame(
        {"centroid_lon": [c[0] for c in coords], "centroid_lat": [c[1] for c in coords]},
        geometry=[Point(*c) for c in coords], crs=4326,
    )
    if covered is not None:
        g["tsunami_covered"] = covered
    return g


def test_disabled_sets_na_false():
    out = apply_coastal_scope(_gdf([(139.7, 35.6)]), "tsunami", enabled=False)
    assert "tsunami_na" in out.columns and bool(out["tsunami_na"].iloc[0]) is False


def test_high_ground_uncovered_in_covered_city_is_na():
    # one building officially covered (city has official data), the other high & uncovered
    g = _gdf([(139.70, 35.60), (139.71, 35.61)], covered=[True, False])
    out = apply_coastal_scope(g, "tsunami", enabled=True, fetch=lambda u: _HIGH)
    assert bool(out["tsunami_na"].iloc[0]) is False  # covered → never 対象外
    assert bool(out["tsunami_na"].iloc[1]) is True    # high & uncovered → 対象外


def test_low_ground_uncovered_in_covered_city_stays_no_data():
    g = _gdf([(139.70, 35.60), (139.71, 35.61)], covered=[True, False])
    out = apply_coastal_scope(g, "tsunami", enabled=True, fetch=lambda u: _LOW)
    assert bool(out["tsunami_na"].iloc[1]) is False  # low-lying & uncovered → honest no-data


def test_low_ground_no_coverage_stays_no_data_not_na():
    # HONESTY: a low building with no official 想定 must NOT be marked 対象外
    # (a 0 m waterfront ward is genuinely uncertain, not "out of scope").
    g = _gdf([(139.70, 35.60), (139.71, 35.61)], covered=[False, False])
    out = apply_coastal_scope(g, "tsunami", enabled=True, fetch=lambda u: _LOW)
    assert not out["tsunami_na"].any()  # low & uncovered → honest no-data, never 対象外


def test_high_ground_no_coverage_is_na():
    g = _gdf([(139.70, 35.60)], covered=None)  # no coverage column at all
    out = apply_coastal_scope(g, "storm_surge", enabled=True, fetch=lambda u: _HIGH)
    assert bool(out["storm_surge_na"].iloc[0]) is True  # above max surge reach → 対象外
    names = set(BUILDINGS_ARROW_SCHEMA.names)
    assert "tsunami_na" in names and "storm_surge_na" in names
