"""End-to-end Gate A integration smoke test using a synthetic fixture.

We don't run the real nusamai binary or download anything. Instead we
substitute the conversion + download steps with a tiny GeoJSON fixture and
assert that the full Gate A produces a parquet that passes ``verify``.

This is the test that would have caught every "I forgot a column" /
"feature.id wasn't restored" / "manifest count mismatch" bug.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Polygon, mapping

from plateau_parquet.pipeline import gate_a as gate_a_module
from plateau_parquet.pipeline.gate_a import run_gate_a
from plateau_parquet.sources.citygml import ConvertResult
from plateau_parquet.sources.hazard import HazardLayer
from plateau_parquet.verify import verify


def _make_geojson(path: Path, features: list[dict]) -> None:
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))


@pytest.fixture()
def fixture_dir(tmp_path: Path) -> Path:
    """Build a tiny synthetic dataset on disk and return its directory."""
    fix = tmp_path / "fixture"
    fix.mkdir()

    # 3 buildings: one wooden pre-1981 inside any future flood polygon,
    # one RC, one with all-unknown attrs.
    g1 = Polygon([(139.700, 35.660), (139.701, 35.660), (139.701, 35.661), (139.700, 35.661)])
    g2 = Polygon([(139.702, 35.660), (139.703, 35.660), (139.703, 35.661), (139.702, 35.661)])
    g3 = Polygon([(139.704, 35.660), (139.705, 35.660), (139.705, 35.661), (139.704, 35.661)])
    _make_geojson(
        fix / "bldg.geojson",
        [
            {
                "type": "Feature",
                "id": "bldg_a",
                "properties": {
                    "yearOfConstruction": 1975,
                    "measuredHeight": 7.5,
                    "storeysAboveGround": 2,
                    "usage": ["411"],
                    "buildingDetailAttribute": [{"buildingStructureType": "611"}],
                },
                "geometry": mapping(g1),
            },
            {
                "type": "Feature",
                "id": "bldg_b",
                "properties": {
                    "yearOfConstruction": 2015,
                    "measuredHeight": 25.0,
                    "storeysAboveGround": 7,
                    "usage": ["421"],
                    "buildingDetailAttribute": [{"buildingStructureType": "613"}],
                },
                "geometry": mapping(g2),
            },
            {"type": "Feature", "id": "bldg_c", "properties": {}, "geometry": mapping(g3)},
        ],
    )
    return fix


def test_gate_a_end_to_end_via_fixture(
    fixture_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bypass converter + downloader; run Gate A on synthetic data."""
    out_dir = tmp_path / "out"

    # --- Stub the I/O layer. ---
    bldg_geojson = fixture_dir / "bldg.geojson"

    def fake_fetch(url: str, cache_dir: Path, timeout: float = 60.0) -> Path:
        # All datasets are "downloaded" by returning the fixture dir.
        return fixture_dir

    def fake_convert(citygml_dir, out_dir, **kw) -> ConvertResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / "buildings.geojson"
        target.write_text(bldg_geojson.read_text())
        return ConvertResult(geojson_path=target, tiles3d_dir=None)

    flood_poly = Polygon(
        [(139.6995, 35.6595), (139.7015, 35.6595), (139.7015, 35.6615), (139.6995, 35.6615)]
    )

    def fake_load_hazard(entry, src, work_dir, **kw) -> HazardLayer:
        gdf = gpd.GeoDataFrame({"depth_m": [2.5]}, geometry=[flood_poly], crs="EPSG:4326")
        return HazardLayer(kind=entry.hazard_kind, inundation_gdf=gdf, source_id=entry.dataset_id)

    monkeypatch.setattr(gate_a_module, "fetch_and_unzip", fake_fetch)
    monkeypatch.setattr(gate_a_module, "convert_buildings", fake_convert)
    monkeypatch.setattr(gate_a_module, "load_hazard", fake_load_hazard)
    # coverage resolver: pretend every hazard has an explicit extent covering all three buildings.
    extent_gdf = gpd.GeoDataFrame(
        geometry=[
            Polygon(
                [(139.6990, 35.6590), (139.7060, 35.6590), (139.7060, 35.6620), (139.6990, 35.6620)]
            )
        ],
        crs="EPSG:4326",
    )

    from plateau_parquet.schema import CoverageConfidence
    from plateau_parquet.sources.coverage import CoverageExtent

    def fake_resolve_coverage(entry, admin_boundary, **kw):
        return CoverageExtent(
            kind=entry.hazard_kind,
            source_id=entry.dataset_id,
            geometry=extent_gdf,
            confidence=CoverageConfidence.EXPLICIT_POLYGON,
        )

    monkeypatch.setattr(gate_a_module, "resolve_coverage", fake_resolve_coverage)

    # --- Build a synthetic catalog. ---
    from plateau_parquet.catalog import CityCatalog, DatasetEntry
    from plateau_parquet.schema import HazardKind

    catalog = CityCatalog(
        city_code="99999",
        city_name="Testopolis",
        dataset_year=2024,
        entries=[
            DatasetEntry(
                dataset_id="bldg-99999-2024",
                theme="building",
                year=2024,
                url="http://example.test/bldg.zip",
            ),
            DatasetEntry(
                dataset_id="fld-99999-2024",
                theme="hazard",
                hazard_kind=HazardKind.RIVER_FLOOD,
                year=2024,
                url="http://example.test/fld.zip",
            ),
        ],
    )

    res = run_gate_a(catalog, out_dir, emit_3dtiles=False)
    assert res.buildings_parquet.exists()
    assert res.manifest_path.exists()

    # `verify` should report no errors.
    report = verify(out_dir)
    assert report.n_buildings == 3
    assert not report.errors, [(f.code, f.message) for f in report.errors]

    # Honesty check: bldg_a is inside the flood polygon (covered + hit),
    # bldg_b/c outside flood polygon but still inside coverage extent.
    gdf = gpd.read_parquet(res.buildings_parquet)
    a = gdf[gdf["gml_id"] == "bldg_a"].iloc[0]
    b = gdf[gdf["gml_id"] == "bldg_b"].iloc[0]
    assert bool(a["river_flood_covered"]) is True
    assert float(a["river_flood_depth_max"]) == 2.5
    assert bool(b["river_flood_covered"]) is True
    import math
    assert math.isnan(float(b["river_flood_depth_max"]))
