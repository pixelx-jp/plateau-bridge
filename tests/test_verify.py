"""`plateau verify` must catch the honesty violation: covered=false + depth>0."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Polygon

from plateau_parquet import __version__
from plateau_parquet.schema import (
    CoverageConfidence,
    CoverageStats,
    HazardKind,
    Manifest,
    SourceRef,
)
from plateau_parquet.verify import verify


def _make_bundle(tmp_path: Path, rows: list[dict]) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    gdf = gpd.GeoDataFrame(rows, geometry=[Polygon([(0, 0), (1, 0), (1, 1)])] * len(rows), crs="EPSG:4326")
    gdf.to_parquet(out / "buildings.parquet", index=False)
    manifest = Manifest(
        tool_version=__version__,
        generated_at=datetime.now(tz=UTC),
        city_code="99999",
        dataset_year=2024,
        n_buildings=len(rows),
        datasets=["fld-99999"],
        sources={
            "fld-99999": SourceRef(
                source_id="fld-99999",
                dataset_id="fld-99999",
                year=2024,
                url="http://example.test",
            )
        },
        coverage_stats=[
            CoverageStats(
                kind=HazardKind.RIVER_FLOOD,
                covered_count=1,
                hit_count=1,
                coverage_confidence_breakdown={CoverageConfidence.EXPLICIT_POLYGON: 1},
            )
        ],
        field_coverage={},
    )
    (out / "manifest.json").write_text(manifest.model_dump_json(indent=2))
    return out


def _baseline_row(building_uid: str, **overrides) -> dict:
    base: dict = {
        "building_uid": building_uid,
        "gml_id": building_uid,
        "city_code": "99999",
        "dataset_year": 2024,
        "source_file_id": "f",
        "centroid_lat": 0.5,
        "centroid_lon": 0.5,
        "year_built": None,
        "structure": None,
        "usage": None,
        "height": None,
        "floors_above": None,
        "floors_below": None,
        "fire_resistance": None,
        "zoning_use": None,
        "far_max": None,
        "tile_content_uri": None,
        "tile_feature_id": None,
        "source_url": "http://example.test",
        "source_dataset_id": "fld-99999",
        "attribution": "© Project PLATEAU / MLIT (CC BY 4.0)",
    }
    for kind in HazardKind:
        base[f"{kind.value}_covered"] = False
        base[f"{kind.value}_coverage_source_ids"] = ""
        base[f"{kind.value}_coverage_confidence"] = "unknown"
        base[f"{kind.value}_hit_source_ids"] = ""
        if kind == HazardKind.LANDSLIDE:
            base[f"{kind.value}_in_zone"] = False
        else:
            base[f"{kind.value}_depth_max"] = None
    base.update(overrides)
    return base


def test_verify_catches_honesty_violation(tmp_path: Path) -> None:
    out = _make_bundle(
        tmp_path,
        [
            _baseline_row(
                "u1",
                river_flood_covered=False,  # <-- not covered
                river_flood_depth_max=2.5,  # but a depth! → violation
            )
        ],
    )
    report = verify(out)
    codes = [f.code for f in report.findings]
    assert "honesty_violation" in codes
    assert not report.ok()


def test_verify_passes_clean_bundle(tmp_path: Path) -> None:
    out = _make_bundle(
        tmp_path,
        [
            _baseline_row(
                "u1",
                river_flood_covered=True,
                river_flood_coverage_source_ids="fld-99999",
                river_flood_coverage_confidence="explicit_polygon",
                river_flood_depth_max=2.5,
                river_flood_hit_source_ids="fld-99999",
            )
        ],
    )
    report = verify(out)
    assert report.ok(), [f.code for f in report.errors]


def test_verify_catches_duplicate_uid(tmp_path: Path) -> None:
    out = _make_bundle(tmp_path, [_baseline_row("u1"), _baseline_row("u1")])
    report = verify(out)
    assert any(f.code == "uid_not_unique" for f in report.findings)
