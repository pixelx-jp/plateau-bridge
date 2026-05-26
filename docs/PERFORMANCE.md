# Performance notes

Numbers from a real Apple M-series laptop running real PLATEAU data. Updated
when measurements drift.

## Build wall-clock by city

| Stage | Shibuya 13113 (41,858 bldgs) | Shinjuku 13104 (57,485) | Osaka 27100 (615,513) |
|---|---:|---:|---:|
| Download CityGML zip | ~90 s (636 MB) | ~110 s (777 MB) | ~200 s (1.5 GB) |
| Unzip | ~6 s | ~7 s | ~25 s |
| nusamai → GeoJSON | ~30 s | ~35 s | ~120 s |
| Load GeoJSON into geopandas | ~5 s | ~6 s | ~38 s |
| Admin-clip buildings | ~0.5 s | ~0.6 s | ~6 s |
| **Hazard intersection (sjoin, centroid mode)** | **~3 s** | **~4 s** | **~3 min 45 s** |
| nusamai → 3D Tiles | ~3 min | ~3 min | ~10 min (reuses cache) |
| Read 3D Tiles metadata | ~10 s | ~12 s | ~60 s |
| PMTiles (tippecanoe) | ~15 s | ~18 s | (TBD) |
| FGB write | ~5 s | ~6 s | ~30 s |

End-to-end Gate A→B→C:
- Shibuya: **~5 minutes** cold, ~3 min warm
- Shinjuku: **~6 minutes** cold
- Osaka with `--no-hazards`: **~12 minutes** cold (mostly nusamai 3D Tiles)
- Osaka **with** hazards: **~75–110 minutes** cold (mostly libgeos sjoin)

## Centroid-mode spatial join (the 120× speedup)

PLATEAU's prefecture-wide tsunami / storm_surge layers ship as single
mega-polygons with hundreds of thousands of vertices. With the polygon-vs-
polygon `intersects` predicate (libgeos `PreparedPolygon::intersects`), the
work is O(N · M) where M is the giant polygon's vertex count — Osaka 615 k
buildings × ~500 k vertex tsunami polygon ran for **7 h 45 min** before we
killed it.

`apply_coverage` / `apply_hazards` default to **centroid mode**: replace
each building's footprint with its `representative_point()` and use the
`within` predicate. Same STRtree spatial index, but now O(N · log M) per
layer because Point-in-Polygon is a binary search through the polygon's
edges. Same Osaka run took **3 min 45 s** verified end-to-end — ~120 ×
speedup.

Precision cost: a building's centroid vs its footprint edge differ by at
most half a footprint width (≈5–10 m for typical 大阪市 plots). PLATEAU
hazard polygons span kilometres and are themselves quantised to ~10 m
resolution upstream, so the precision loss is below the data's accuracy
floor.

`centroid_mode=False` is available for edge-precise legacy behaviour:

```python
from plateau_bridge.ops.intersect import apply_hazards
apply_hazards(gdf, layers, centroid_mode=False)   # legacy
```

## Why Osaka hazard intersection used to be slow

`apply_hazards` uses `geopandas.sjoin(predicate="intersects")` which delegates
to `libgeos`'s `PreparedPolygon::intersects`. Cost is roughly:

```
buildings × (avg candidate polys per bbox) × (avg vertices per poly)
```

For Osaka 大阪市:

- **buildings:** 615,513
- **candidate polys per bbox:** ~10–30 (4 河川流域 + multiple LOD layers)
- **vertices per poly:** 5,000–20,000 (flood polygons span whole river basins)

That's ~50 GB-equivalent of segment-intersection work in libgeos's
`FastSegmentSetIntersectionFinder`. Even with the STRtree spatial index and
PreparedPolygon optimisation, this is genuinely O(N · M / index_efficiency).

Mitigations in the current implementation:
1. **bbox prefilter at read time** (`pyogrio.read_file(bbox=...)`) — cuts the
   raw flood layer from 3.4 GB to ~64k polygons before any sjoin runs.
2. **admin clipping** runs before hazards — Osaka 大阪市 only loses 0.1% of
   buildings, but for prefecture-wide bundles (e.g. Shibuya 90 k → 42 k) the
   reduction is half.
3. **Per-kind isolation** — landslide adds a sjoin pass but with tiny inputs.

Further wins (not yet implemented):
- Centroid-only intersection (`predicate="within"` against pre-prepared
  polygons + centroid-only building geom). Trades a small amount of
  edge-of-polygon precision for ~10× speedup.
- Per-flood-source bbox prefilter (currently the bbox is the whole admin
  envelope; individual flood layers cover smaller sub-basins).
- Parallel sjoin via `dask-geopandas` for cities with >100 k buildings.

If your workflow tolerates the wait, just call:

```bash
plateau build 27100 --no-hazards         # ~12 min, ships parquet + 3D Tiles
plateau hazard 27100                     # ~60–90 min, adds hazards in place
```

Both stages are independently checkpointed so you can ship Gate A/B before
the hazard join finishes.

## DuckDB query latency (Apple M-series)

From `plateau bench out/buildings.parquet -n 20` against Shibuya
(41,858 rows, 62 cols, 20 MB GeoParquet):

| Query | median ms | p99 ms |
|---|---:|---:|
| `filter_by_attrs`     | 0.31 | 0.56 |
| `decade_histogram`    | 0.32 | 0.39 |
| `river_flood_at_risk` | 0.58 | 0.69 |
| `centroid_table`      | 1.70 | 1.89 |
| `bbox_count`          | 1.18 | 1.29 |
| `honesty_pivot`       | 1.09 | 1.23 |

All sub-2 ms. The 1,000-row `centroid_table` is the only one that's
materialisation-bound; the others read columnar stats and return single
scalars.

### Scaling to 615 k rows (Osaka)

Same suite against Osaka 27100 (615,513 rows, 304 MB GeoParquet):

| Query | median ms | p99 ms |
|---|---:|---:|
| `filter_by_attrs`     | 0.34 | 0.46 |
| `decade_histogram`    | 0.34 | 0.47 |
| `river_flood_at_risk` | 0.28 | 0.42 |
| `centroid_table`      | 2.73 | 2.91 |
| `bbox_count`          | 0.32 | 0.43 |
| `honesty_pivot`       | 3.46 | 3.71 |

15× the rows, ~2× the latency on the materialisation-bound queries; scalar
aggregates stay flat (columnar stats are O(1) per row group). DuckDB scales
linearly with the work, not with the parquet size.

## Disk footprint

| Artifact (Shibuya) | Size |
|---|---:|
| `buildings.parquet` | 20 MB |
| `buildings.pmtiles` | 13 MB |
| `buildings/13113.fgb` | 197 MB |
| `3dtiles/` (2,285 glb files) | 1.8 GB |
| `style/` (875 Arrow files) | ~18 MB |
| `_work/` (intermediate) | ~3 GB |

For Osaka the `3dtiles/` ballooning to ~8 GB and the WaterBody.geojson
intermediate at 3.4 GB are the two big-disk items — the `_work/` dir is
safe to delete once `verify` passes.

## GDAL gotcha: OGR_GEOJSON_MAX_OBJ_SIZE

GDAL's GeoJSON driver caps any single feature at 200 MB by default. PLATEAU's
prefecture-wide flood layers exceed this for big cities — Osaka 大阪市
tsunami/storm_surge each ship as one ~180-400 MB feature. The driver returns
`GeoJSON object too complex/large`. `sources/citygml.py` sets
`OGR_GEOJSON_MAX_OBJ_SIZE=0` at import time to disable the cap.

If you see the error, check your env doesn't override it back to a finite
value, or upgrade GDAL ≥ 3.9 where the default was lifted.

## Memory ceiling

geopandas / libgeos hold ~12 GB peak resident on Osaka during the sjoin.
30 GB free RAM is comfortable; 16 GB Macs should `--no-hazards` and run
hazard separately.
