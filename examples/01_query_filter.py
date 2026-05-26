"""Five reference queries against buildings.parquet.

Run after `plateau build --city 13113 --out ./out`.

These are the queries downstream apps will issue most often. If you can run
them all, your parquet is healthy.
"""

from __future__ import annotations

import sys

import duckdb
import geopandas as gpd

PARQUET = sys.argv[1] if len(sys.argv) > 1 else "out/buildings.parquet"


def q1_filter_by_attrs() -> None:
    """All wooden buildings older than 1981 in Shibuya."""
    df = duckdb.sql(f"""
        SELECT building_uid, year_built, structure
        FROM '{PARQUET}'
        WHERE city_code = '13113'
          AND structure = 'wood'
          AND year_built < 1981
    """).df()
    print(df.head())


def q2_color_by_year() -> None:
    """Year-built histogram — the basis of prettyplateau's 'Building Age Rainbow'."""
    df = duckdb.sql(f"""
        SELECT FLOOR(year_built / 10) * 10 AS decade, COUNT(*) AS n
        FROM '{PARQUET}'
        WHERE year_built IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """).df()
    print(df)


def q3_hazard_intersection() -> None:
    """Buildings that *actually* hit a river-flood zone (covered & depth > 0)."""
    df = duckdb.sql(f"""
        SELECT COUNT(*) AS n_at_risk
        FROM '{PARQUET}'
        WHERE river_flood_covered AND river_flood_depth_max > 1.0
    """).df()
    print(df)


def q4_centroid_for_osm() -> None:
    """Centroid table — what you'd JOIN against OSM at runtime."""
    df = duckdb.sql(f"""
        SELECT building_uid, centroid_lon, centroid_lat
        FROM '{PARQUET}'
        LIMIT 5
    """).df()
    print(df)


def q5_bbox_via_geopandas() -> None:
    """bbox query — Risk Lens calls this with a user-drawn rectangle."""
    gdf = gpd.read_parquet(PARQUET)
    bbox = (139.70, 35.65, 139.71, 35.66)
    sub = gdf.cx[bbox[0]:bbox[2], bbox[1]:bbox[3]]
    print(f"{len(sub)} buildings in bbox")


if __name__ == "__main__":
    q1_filter_by_attrs()
    q2_color_by_year()
    q3_hazard_intersection()
    q4_centroid_for_osm()
    q5_bbox_via_geopandas()
