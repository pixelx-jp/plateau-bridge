"""Attribute normalisation against the *real* nusamai GeoJSON property shape.

Keys are camelCase, namespace dropped; Code is a bare string; arrays of Code
are arrays of strings; ``buildingDetailAttribute`` is a nested array of
dicts. These fixtures encode that contract — if nusamai changes its emit
shape, this is what trips.
"""

from __future__ import annotations

import geopandas as gpd
from shapely.geometry import Polygon

from plateau_parquet.ops.attributes import field_coverage, normalise


def _row(geom: Polygon, **props):
    return {"geometry": geom, **props}


def test_normalise_extracts_real_nusamai_keys() -> None:
    """nusamai resolves codelists to Japanese descriptions and JSON-encodes
    nested attributes. Pin both behaviours so a converter upgrade trips here."""
    import json
    g = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    df = gpd.GeoDataFrame(
        [
            _row(
                g,
                gml_id="bldg_001",
                yearOfConstruction=1975,
                measuredHeight=8.2,
                storeysAboveGround=2,
                storeysBelowGround=0,
                # Vec<Code> arrives as a JSON-string of an array of descriptions.
                usage=json.dumps(["住宅"]),
                buildingDetailAttribute=json.dumps([
                    {"buildingStructureType": "木造", "fireproofStructureType": "準耐火造"}
                ]),
            ),
            _row(
                g,
                gml_id="bldg_002",
                yearOfConstruction=2015,
                measuredHeight=42.0,
                storeysAboveGround=11,
                storeysBelowGround=2,
                usage=json.dumps(["事務所"]),
                buildingDetailAttribute=json.dumps([
                    {"buildingStructureType": "鉄筋コンクリート造"}
                ]),
            ),
            _row(g, gml_id="bldg_003"),  # all unknown
        ],
        crs="EPSG:4326",
    )

    out = normalise(df)

    import pytest
    assert int(out.iloc[0]["year_built"]) == 1975
    assert float(out.iloc[0]["height"]) == pytest.approx(8.2, rel=1e-5)
    assert int(out.iloc[0]["floors_above"]) == 2
    assert out.iloc[0]["usage"] == "residential"
    assert out.iloc[0]["structure"] == "wood"
    assert out.iloc[0]["fire_resistance"] == "準耐火造"

    assert int(out.iloc[1]["year_built"]) == 2015
    assert out.iloc[1]["usage"] == "commercial"
    assert out.iloc[1]["structure"] == "rc"

    # Row 3: missing everything → nullable Int/Float with NA, usage default
    # "other" (because USAGE_MAP fillna), structure default "other" too.
    import pandas as pd
    assert pd.isna(out.iloc[2]["year_built"])
    assert pd.isna(out.iloc[2]["height"])


def test_structure_stays_none_when_buildingStructureType_is_empty() -> None:
    """Real PLATEAU Shibuya 2023: ``buildingStructureType`` is 0% populated.
    The pipeline must report null structure, not silently default every row
    to 'other' — the manifest's field_coverage depends on this honesty."""
    import json
    g = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    df = gpd.GeoDataFrame(
        [
            _row(g, gml_id="b", buildingDetailAttribute=json.dumps([
                {"fireproofStructureType": "耐火"}   # no buildingStructureType
            ])),
            _row(g, gml_id="c", buildingDetailAttribute=json.dumps([
                {"fireproofStructureType": "その他"}
            ])),
        ],
        crs="EPSG:4326",
    )
    out = normalise(df)
    assert out["structure"].isna().all()
    assert list(out["fire_resistance"]) == ["耐火", "その他"]


def test_field_coverage_handles_objects_and_lists() -> None:
    g = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    df = gpd.GeoDataFrame(
        [
            {"geometry": g, "year_built": 1990, "usage": "residential"},
            {"geometry": g, "year_built": None, "usage": ""},
            {"geometry": g, "year_built": 2010, "usage": "commercial"},
        ],
        crs="EPSG:4326",
    )
    cov = field_coverage(df, ["year_built", "usage", "nonexistent"])
    assert cov["year_built"] == 2 / 3
    assert cov["usage"] == 2 / 3
    assert cov["nonexistent"] == 0.0
