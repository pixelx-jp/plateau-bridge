"""Bundled Tokyo admin polygons must be discoverable for the pilot cities."""

from __future__ import annotations

from plateau_bridge.admin import _bundled_admin, load_admin


def test_bundled_admin_loads() -> None:
    gdf = _bundled_admin()
    assert "city_code" in gdf.columns
    # All 8 catalog-bundled cities must have a polygon.
    codes = set(gdf["city_code"].astype(str))
    for code in ("13113", "13104", "14100", "14204", "23100",
                 "27100", "40130", "01100"):
        assert code in codes, f"missing admin polygon for {code}"


def test_all_catalog_cities_have_admin() -> None:
    """Every city in the bundled catalog must have a usable admin polygon."""
    from plateau_bridge.catalog import load_registry
    for (code, _year), _cat in load_registry().items():
        g = load_admin(code)
        assert g is not None, f"missing admin polygon for catalog city {code}"
        assert len(g) == 1
        assert g.crs is not None


def test_load_admin_returns_single_row_for_shibuya() -> None:
    g = load_admin("13113")
    assert g is not None
    assert len(g) == 1
    assert g.crs is not None
    # Shibuya extent — generous bounds check.
    minx, miny, maxx, maxy = g.total_bounds
    assert 139.6 < minx < 139.7
    assert 35.6 < miny < 35.7
    assert 139.7 < maxx < 139.75
    assert 35.65 < maxy < 35.72


def test_load_admin_returns_none_for_unknown_city() -> None:
    assert load_admin("99999") is None


def test_bundled_admin_polygons_are_unionable() -> None:
    """Every admin polygon must survive a ``union_all`` round-trip.

    Edogawa-ku (13123) previously failed with
    ``GEOSException: TopologyException: unable to assign free hole to a
    shell at 139.866562 35.636898`` because its bundled polygon had a
    minor topology defect. ``_bundled_admin`` now calls ``make_valid``
    at load time. This test pins that behaviour — losing it means
    Edogawa (and any future similar case) silently breaks Gate A
    again.
    """
    from plateau_bridge.catalog import load_registry
    for (code, _year), _cat in load_registry().items():
        g = load_admin(code)
        assert g is not None
        # union_all is what gate_a.py:94 actually calls.
        g.geometry.union_all()
