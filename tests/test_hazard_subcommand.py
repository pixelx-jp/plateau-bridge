"""``plateau hazard`` integration: re-runs intersection on an existing parquet
without redoing Gate A. Mocks the I/O layer so the test doesn't touch the
network or nusamai."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Polygon

from plateau_parquet.catalog import CityCatalog, DatasetEntry
from plateau_parquet.pipeline import hazard_only as ho_module
from plateau_parquet.pipeline.hazard_only import run_hazard_only
from plateau_parquet.schema import CoverageConfidence, HazardKind
from plateau_parquet.sources.coverage import CoverageExtent
from plateau_parquet.sources.hazard import HazardLayer


def _polygon(x: float, y: float, size: float = 0.01) -> Polygon:
    return Polygon([(x, y), (x + size, y), (x + size, y + size), (x, y + size)])


def test_plateau_hazard_adds_columns_without_redoing_gate_a(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pre-existing Gate-A parquet, no hazards yet (skip_hazards=True path).
    rows = []
    for i, code in enumerate(["a", "b", "c"]):
        rows.append({
            "building_uid": code,
            "gml_id": code,
            "city_code": "99999",
            "dataset_year": 2024,
            "source_file_id": "f",
            "centroid_lat": 35.0 + i * 0.0001,
            "centroid_lon": 139.0 + i * 0.0001,
            "attribution": "© Project PLATEAU / MLIT (CC BY 4.0)",
            "source_url": "http://example.test",
            "source_dataset_id": "f",
            "tile_content_uri": f"t{i}.glb",   # preserved from Gate B
            "tile_feature_id": i,
        })
    gdf = gpd.GeoDataFrame(
        rows,
        geometry=[_polygon(139.0, 35.0), _polygon(139.0001, 35.0001), _polygon(140.0, 36.0)],
        crs="EPSG:4326",
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    gdf.to_parquet(out_dir / "buildings.parquet", index=False)

    # Synthetic catalog + admin polygon + hazard layer.
    flood_poly = _polygon(138.99, 34.99, 0.05)
    catalog = CityCatalog(
        city_code="99999",
        city_name="Test",
        dataset_year=2024,
        entries=[
            DatasetEntry(
                dataset_id="bldg-99999",
                theme="building",
                year=2024,
                url="http://example.test/bldg.zip",
            ),
            DatasetEntry(
                dataset_id="fld-99999",
                theme="hazard",
                hazard_kind=HazardKind.RIVER_FLOOD,
                year=2024,
                url="http://example.test/fld.zip",
                declared_full_admin=True,
            ),
        ],
    )

    def fake_fetch(url, cache_dir, timeout: float = 60.0):
        d = tmp_path / "src"
        d.mkdir(exist_ok=True)
        return d

    def fake_load_hazard(entry, src, work_dir, **kw):
        flood = gpd.GeoDataFrame(
            {"depth_m": [3.0]},
            geometry=[flood_poly],
            crs="EPSG:4326",
        )
        return HazardLayer(kind=entry.hazard_kind, inundation_gdf=flood, source_id=entry.dataset_id)

    def fake_resolve_coverage(entry, admin_boundary, **kw):
        return CoverageExtent(
            kind=entry.hazard_kind,
            source_id=entry.dataset_id,
            geometry=gpd.GeoDataFrame(geometry=[flood_poly], crs="EPSG:4326"),
            confidence=CoverageConfidence.EXPLICIT_POLYGON,
        )

    monkeypatch.setattr(ho_module, "fetch_and_unzip", fake_fetch)
    monkeypatch.setattr(ho_module, "load_hazard", fake_load_hazard)
    monkeypatch.setattr(ho_module, "resolve_coverage", fake_resolve_coverage)

    res = run_hazard_only(catalog, out_dir)
    assert res.n_buildings == 3

    # Tile keys preserved — this is the regression we explicitly fixed.
    out = gpd.read_parquet(out_dir / "buildings.parquet")
    assert list(out["tile_content_uri"]) == ["t0.glb", "t1.glb", "t2.glb"]
    assert list(out["tile_feature_id"]) == [0, 1, 2]

    # Honesty: building 'c' (far outside the flood polygon) MUST stay uncovered.
    by_uid = out.set_index("building_uid")
    assert bool(by_uid.loc["a", "river_flood_covered"]) is True
    assert bool(by_uid.loc["c", "river_flood_covered"]) is False
    import math
    assert math.isnan(float(by_uid.loc["c", "river_flood_depth_max"]))
