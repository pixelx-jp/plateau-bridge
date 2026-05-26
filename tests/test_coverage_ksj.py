"""Pin the KSJ-mapping + metadata-XML join path.

These tests verify the *infrastructure* (loader, schema validation,
metadata-driven dispatch). They don't hit the network — KSJ download
itself is exercised via a stubbed cache directory.

The contract this test pins down:
- An empty `coverage_sources.json` → `load_coverage_sources()` returns
  empty dict (community-driven curation; clean fall-through).
- A row with a hazard kind that doesn't match an entry's hazard is
  skipped, NOT mismatched.
- A row with malformed `hazard` is dropped with a warning, NOT crashed.
- Metadata XML parsing → canonicalisation → dict lookup is one pipeline.
"""
from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from plateau_bridge.catalog import DatasetEntry
from plateau_bridge.schema import HazardKind
from plateau_bridge.sources.coverage_ksj import (
    KsjMapping,
    load_coverage_sources,
    resolve_explicit_polygon_from_metadata,
)


def _entry(hazard: HazardKind) -> DatasetEntry:
    return DatasetEntry(
        dataset_id=f"test-{hazard.value}",
        theme="hazard",
        hazard_kind=hazard,
        year=2024,
        url="https://example.invalid/x.zip",
    )


METADATA_XML = dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <MD_Metadata xmlns="http://zgate.gsi.go.jp/ch/jmp/">
      <identificationInfo>
        <MD_DataIdentification>
          <citation>
            <title>洪水浸水想定区域3Dモデル_13115_city_2024_op</title>
          </citation>
          <descriptiveKeywords>
            <MD_Keywords>
              <keyword>利根川水系利根川洪水浸水想定区域図（平成29年7月20日）国土交通省関東地方整備局利根川下流河川事務所</keyword>
              <keyword>多摩川水系多摩川、浅川、大栗川洪水浸水想定区域図（平成28年5月30日）国土交通省関東地方整備局京浜河川事務所</keyword>
              <type>005</type>
            </MD_Keywords>
          </descriptiveKeywords>
        </MD_DataIdentification>
      </identificationInfo>
    </MD_Metadata>
""")


def test_load_coverage_sources_returns_empty_for_empty_table() -> None:
    """The bundled coverage_sources.json ships with `entries: {}`. Empty
    table means callers fall through cleanly — never a crash."""
    mapping = load_coverage_sources()
    # Hosted table may have grown by the time we add real entries; but
    # the type must be dict, not None.
    assert isinstance(mapping, dict)


def test_resolve_returns_none_when_table_empty(tmp_path: Path) -> None:
    """An empty mapping → resolver no-ops with None → caller drops to
    declared_full_admin. The single most important invariant: when
    nobody's mapped any source, we DO NOT regress to unknown — we
    just don't upgrade beyond declared_full_admin."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    meta = bundle / "metadata"
    meta.mkdir()
    (meta / "udx_13115_city_2024_fld_op.xml").write_text(METADATA_XML, encoding="utf-8")

    with patch(
        "plateau_bridge.sources.coverage_ksj.load_coverage_sources",
        return_value={},
    ):
        result = resolve_explicit_polygon_from_metadata(
            _entry(HazardKind.RIVER_FLOOD),
            bundle,
            tmp_path / "ksj_cache",
        )
    assert result is None


def test_resolve_returns_none_when_no_mapping_matches(tmp_path: Path) -> None:
    """A non-empty mapping with no matching source-document also no-ops."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    meta = bundle / "metadata"
    meta.mkdir()
    (meta / "udx_13115_city_2024_fld_op.xml").write_text(METADATA_XML, encoding="utf-8")

    mapping = {
        "鹿児島県某河川洪水浸水想定区域図": KsjMapping(
            hazard=HazardKind.RIVER_FLOOD,
            ksj_urls=("https://example.invalid/unrelated.zip",),
            published="2020-01-01",
        ),
    }
    with patch(
        "plateau_bridge.sources.coverage_ksj.load_coverage_sources",
        return_value=mapping,
    ):
        result = resolve_explicit_polygon_from_metadata(
            _entry(HazardKind.RIVER_FLOOD),
            bundle,
            tmp_path / "ksj_cache",
        )
    assert result is None


def test_resolve_skips_wrong_hazard_kind(tmp_path: Path) -> None:
    """A row tagged ``tsunami`` must NOT match a ``river_flood`` entry,
    even if the canonicalised source name is identical."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    meta = bundle / "metadata"
    meta.mkdir()
    (meta / "udx_13115_city_2024_fld_op.xml").write_text(METADATA_XML, encoding="utf-8")

    mapping = {
        "利根川水系利根川洪水浸水想定区域図": KsjMapping(
            hazard=HazardKind.TSUNAMI,  # wrong kind for this entry
            ksj_urls=("https://example.invalid/x.zip",),
            published="2017-07-20",
        ),
    }
    with patch(
        "plateau_bridge.sources.coverage_ksj.load_coverage_sources",
        return_value=mapping,
    ):
        result = resolve_explicit_polygon_from_metadata(
            _entry(HazardKind.RIVER_FLOOD),
            bundle,
            tmp_path / "ksj_cache",
        )
    assert result is None


def test_resolver_falls_back_when_ksj_insufficient_vs_hazard(tmp_path: Path) -> None:
    """Sanity check: if KSJ-derived polygon covers < 95% of PLATEAU hazard
    polygons' area, resolve_coverage must NOT use it.

    With ``hazard_polygons`` present and KSJ rejected, the resolver falls
    to ``INUNDATION_BOUNDED`` — using the hazard polygons themselves as
    the extent. Strictly more honest than ``declared_full_admin``, which
    would overclaim "modelled-and-safe" for buildings outside the
    modelling area.
    """
    import geopandas as gpd
    from shapely.geometry import box

    from plateau_bridge.schema import CoverageConfidence
    from plateau_bridge.sources.coverage import resolve_coverage

    entry = DatasetEntry(
        dataset_id="t-fld",
        theme="hazard",
        hazard_kind=HazardKind.RIVER_FLOOD,
        year=2024,
        url="https://example.invalid/x.zip",
        declared_full_admin=True,
    )
    # Admin = 10x10 square; hazard polygons fill 8x10 of it; KSJ "extent" only
    # covers a 2x10 sliver. Coverage of hazard = 25%, well under 95%.
    admin = gpd.GeoDataFrame({"geometry": [box(0, 0, 10, 10)]}, crs=4326)
    hazard = gpd.GeoDataFrame({"geometry": [box(0, 0, 8, 10)]}, crs=4326)
    sliver = gpd.GeoDataFrame({"geometry": [box(0, 0, 2, 10)]}, crs=4326)

    from plateau_bridge.sources.coverage_ksj import _ExplicitPolygonResult
    fake_result = _ExplicitPolygonResult(polygon=sliver, source_documents=("test",))
    with patch(
        "plateau_bridge.sources.coverage_ksj.resolve_explicit_polygon_from_metadata",
        return_value=fake_result,
    ):
        ext = resolve_coverage(
            entry, admin,
            dataset_root=tmp_path,            # non-None enables KSJ step
            hazard_polygons=hazard,
        )
    # KSJ rejected → INUNDATION_BOUNDED (uses hazard polygons as extent).
    # NOT the sliver, NOT declared_full_admin (which overclaims).
    assert ext is not None
    assert ext.confidence == CoverageConfidence.INUNDATION_BOUNDED
    # Geometry should be the hazard polygons' union, not the sliver.
    assert abs(ext.geometry.geometry.iloc[0].area - 80.0) < 0.01


def test_resolver_falls_back_to_declared_when_no_hazard_polygons(tmp_path: Path) -> None:
    """When ``hazard_polygons`` is NOT provided, KSJ failure must fall
    to ``declared_full_admin`` (the old behaviour)."""
    import geopandas as gpd
    from shapely.geometry import box

    from plateau_bridge.schema import CoverageConfidence
    from plateau_bridge.sources.coverage import resolve_coverage

    entry = DatasetEntry(
        dataset_id="t-fld",
        theme="hazard",
        hazard_kind=HazardKind.RIVER_FLOOD,
        year=2024,
        url="https://example.invalid/x.zip",
        declared_full_admin=True,
    )
    admin = gpd.GeoDataFrame({"geometry": [box(0, 0, 10, 10)]}, crs=4326)

    with patch(
        "plateau_bridge.sources.coverage_ksj.resolve_explicit_polygon_from_metadata",
        return_value=None,
    ):
        ext = resolve_coverage(
            entry, admin,
            dataset_root=tmp_path,
            hazard_polygons=None,  # no hazard polygons available
        )
    assert ext is not None
    assert ext.confidence == CoverageConfidence.DECLARED_FULL_ADMIN


def test_resolver_uses_inundation_bounded_when_no_ksj_match(tmp_path: Path) -> None:
    """No KSJ match + hazard polygons present → INUNDATION_BOUNDED."""
    import geopandas as gpd
    from shapely.geometry import box

    from plateau_bridge.schema import CoverageConfidence
    from plateau_bridge.sources.coverage import resolve_coverage

    entry = DatasetEntry(
        dataset_id="t-fld",
        theme="hazard",
        hazard_kind=HazardKind.RIVER_FLOOD,
        year=2024,
        url="https://example.invalid/x.zip",
        declared_full_admin=True,
    )
    admin = gpd.GeoDataFrame({"geometry": [box(0, 0, 10, 10)]}, crs=4326)
    hazard = gpd.GeoDataFrame({"geometry": [box(2, 2, 5, 5)]}, crs=4326)

    with patch(
        "plateau_bridge.sources.coverage_ksj.resolve_explicit_polygon_from_metadata",
        return_value=None,
    ):
        ext = resolve_coverage(
            entry, admin,
            dataset_root=tmp_path,
            hazard_polygons=hazard,
        )
    assert ext is not None
    assert ext.confidence == CoverageConfidence.INUNDATION_BOUNDED
    assert abs(ext.geometry.geometry.iloc[0].area - 9.0) < 0.01  # 3x3


def test_resolver_accepts_ksj_when_it_dominates_hazard(tmp_path: Path) -> None:
    """The complement: when the KSJ polygon DOES contain the PLATEAU hazard
    polygons, the upgrade goes through normally."""
    import geopandas as gpd
    from shapely.geometry import box

    from plateau_bridge.schema import CoverageConfidence
    from plateau_bridge.sources.coverage import resolve_coverage

    entry = DatasetEntry(
        dataset_id="t-fld",
        theme="hazard",
        hazard_kind=HazardKind.RIVER_FLOOD,
        year=2024,
        url="https://example.invalid/x.zip",
        declared_full_admin=True,
    )
    admin = gpd.GeoDataFrame({"geometry": [box(0, 0, 10, 10)]}, crs=4326)
    hazard = gpd.GeoDataFrame({"geometry": [box(2, 2, 5, 5)]}, crs=4326)
    # KSJ extent fully contains the hazard polygon.
    extent = gpd.GeoDataFrame({"geometry": [box(0, 0, 8, 8)]}, crs=4326)

    from plateau_bridge.sources.coverage_ksj import _ExplicitPolygonResult
    fake_result = _ExplicitPolygonResult(polygon=extent, source_documents=("test",))
    with patch(
        "plateau_bridge.sources.coverage_ksj.resolve_explicit_polygon_from_metadata",
        return_value=fake_result,
    ):
        ext = resolve_coverage(
            entry, admin,
            dataset_root=tmp_path,
            hazard_polygons=hazard,
        )
    assert ext is not None
    assert ext.confidence == CoverageConfidence.EXPLICIT_POLYGON


def test_schema_loader_handles_malformed_hazard(tmp_path: Path) -> None:
    """An entry with an invalid `hazard` value is dropped, not crashed."""
    bad = {
        "_schema_doc": "test",
        "entries": {
            "name1": {"hazard": "not_a_hazard", "ksj_url": "https://x.invalid/y.zip"},
            "name2": {"hazard": "river_flood", "ksj_url": "https://x.invalid/z.zip"},
        },
    }
    bundled = tmp_path / "data" / "coverage_sources.json"
    bundled.parent.mkdir()
    bundled.write_text(json.dumps(bad), encoding="utf-8")

    with patch(
        "plateau_bridge.sources.coverage_ksj.files",
    ) as mock_files:
        # importlib.resources.files-style chain
        class _Loc:
            def __init__(self, p: Path): self.p = p
            def joinpath(self, *parts): return _Loc(self.p.joinpath(*parts))
            def read_text(self, encoding="utf-8"): return self.p.read_text(encoding=encoding)
        mock_files.return_value = _Loc(tmp_path)
        # bust the lru_cache
        load_coverage_sources.cache_clear()
        out = load_coverage_sources()
        load_coverage_sources.cache_clear()
    assert "name1" not in out
    assert "name2" in out
    assert out["name2"].hazard == HazardKind.RIVER_FLOOD
