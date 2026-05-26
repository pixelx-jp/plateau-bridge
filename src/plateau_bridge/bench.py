"""DuckDB query benchmark over ``buildings.parquet``.

``plateau bench out/`` runs a fixed query suite ten times each and reports the
median + p99 latency. The point isn't to beat any specific number — it's so
contributors can ship perf changes with evidence and downstream users can
size their hardware against a published baseline.

Queries are chosen to mirror the five reference patterns in
``examples/02_five_queries.ipynb``: filter, group-by, hazard intersection,
centroid table, bbox.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from pathlib import Path

import duckdb


@dataclass(frozen=True)
class Query:
    name: str
    sql: str


def default_suite(parquet_uri: str) -> list[Query]:
    return [
        Query(
            "filter_by_attrs",
            f"""
            SELECT COUNT(*) FROM '{parquet_uri}'
            WHERE structure = 'wood' AND year_built < 1981
            """,
        ),
        Query(
            "decade_histogram",
            f"""
            SELECT FLOOR(year_built/10)*10 AS decade, COUNT(*) AS n
            FROM '{parquet_uri}'
            WHERE year_built IS NOT NULL
            GROUP BY 1 ORDER BY 1
            """,
        ),
        Query(
            "river_flood_at_risk",
            f"""
            SELECT COUNT(*) FROM '{parquet_uri}'
            WHERE river_flood_covered AND river_flood_depth_max > 1.0
            """,
        ),
        Query(
            "centroid_table",
            f"""
            SELECT building_uid, centroid_lon, centroid_lat
            FROM '{parquet_uri}'
            LIMIT 1000
            """,
        ),
        Query(
            "bbox_count",
            f"""
            SELECT COUNT(*) FROM '{parquet_uri}'
            WHERE centroid_lon BETWEEN 139.69 AND 139.71
              AND centroid_lat BETWEEN 35.65 AND 35.67
            """,
        ),
        Query(
            "honesty_pivot",
            f"""
            SELECT
              SUM(CASE WHEN river_flood_covered AND river_flood_depth_max > 0 THEN 1 ELSE 0 END) AS at_risk,
              SUM(CASE WHEN river_flood_covered AND COALESCE(river_flood_depth_max, 0) = 0 THEN 1 ELSE 0 END) AS surveyed_safe,
              SUM(CASE WHEN NOT river_flood_covered THEN 1 ELSE 0 END) AS unknown
            FROM '{parquet_uri}'
            """,
        ),
    ]


@dataclass(frozen=True)
class BenchResult:
    name: str
    runs: int
    median_ms: float
    p99_ms: float
    rows_returned: int


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = (len(values) - 1) * pct
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return values[f]
    return values[f] + (values[c] - values[f]) * (k - f)


def run_suite(
    parquet_path: Path,
    *,
    iterations: int = 10,
    warmup: int = 1,
) -> list[BenchResult]:
    parquet_uri = str(parquet_path)
    con = duckdb.connect(":memory:")
    # Pre-touch to amortise OS page cache effects across queries.
    con.execute(f"SELECT COUNT(*) FROM '{parquet_uri}'").fetchone()

    out: list[BenchResult] = []
    for q in default_suite(parquet_uri):
        # Warmups don't count.
        for _ in range(warmup):
            con.execute(q.sql).fetchall()
        timings: list[float] = []
        rows = 0
        for _ in range(iterations):
            t0 = time.perf_counter()
            result = con.execute(q.sql).fetchall()
            timings.append((time.perf_counter() - t0) * 1000.0)
            rows = len(result)
        out.append(
            BenchResult(
                name=q.name,
                runs=iterations,
                median_ms=statistics.median(timings),
                p99_ms=_percentile(timings, 0.99),
                rows_returned=rows,
            )
        )
    return out
