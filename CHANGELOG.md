# Changelog

All notable changes to plateau-parquet. We follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and Semantic Versioning.

## [Unreleased]

### 8 cities × full ABC artifact bundle, click-to-inspect, gate B/C resumable

- **All 5 new cities** now have Gate B + Gate C artifacts on disk:
  3D Tiles (3.5–9.5 GB each), per-feature Arrow style tables (1,915 –
  22,039 files), tippecanoe-enriched PMTiles (21–244 MB), per-ward FGB
  (232 MB – 2.6 GB). `plateau verify` 0 errors on every one.
- **`plateau build CITY --gates BC`** now resumable from an existing
  Gate-A parquet: if the parquet exists on disk we skip re-loading
  CityGML and just emit 3D Tiles → style tables → PMTiles → FGB. The
  CLI's gates-only mode now works as documented.
- **Click-to-inspect** in the browser demo: raycast → `_FEATURE_ID_0`
  vertex attribute → per-tile Arrow lookup → info popup. Captured live
  by Playwright (`docs/browser_click.png`): real Shibuya 34.8 m
  commercial building, `building_uid` + `tile_feature_id` + flood
  status all visible.
- **8-city flood hero** at `docs/all_floods.png`: every city's river
  flood map side by side, 1.4 M buildings inside a flood想定 polygon
  nationwide.

### 8 cities all validated end-to-end

Every one of the 8 bundled cities now has a real `buildings.parquet` produced
on this machine (no theory, no claim-without-data). Observed clipped counts:

| city | code | clipped buildings | parquet |
|---|---|---:|---:|
| 札幌市 | 01100 | 646,431 | 299 MB |
| 渋谷区 | 13113 | 41,858 | 20 MB |
| 新宿区 | 13104 | 57,485 | 26 MB |
| 横浜市 | 14100 | **882,831** | 328 MB |
| 鎌倉市 | 14204 | 69,111 | 30 MB |
| 名古屋市 | 23100 | 736,866 | 310 MB |
| 大阪市 | 27100 | 615,513 | 304 MB |
| 福岡市 | 40130 | 355,388 | 145 MB |

**Total: 3,405,483 real buildings, 1.46 GB GeoParquet across 8 cities.**

`docs/multicity.png` updated to show all 8 panels side by side.

The catalog isn't theatre — admin polygons + CKAN URLs work on every city.

### Centroid-mode hazard intersection (120× speedup)

PLATEAU's prefecture-wide tsunami / storm_surge layers ship as single
mega-polygons with hundreds of thousands of vertices. Polygon-vs-polygon
`PreparedPolygon::intersects` against them is O(N · M) and pathologically
slow — 615 k × ~500 k-vertex polygon ran for 7+ hours before being killed.

**Switched the spatial join to use building representative-points vs hazard
polygons with `within`** predicate. Same STRtree, O(N · log M) per layer.
Wall-clock for Osaka full hazard intersection went from **7 h 45 min →
3 min 45 s** (verified). Precision loss bounded by ~half a building
footprint width (~5 m) against hazard polygons that span kilometres —
well below PLATEAU's positional accuracy.

`centroid_mode=True` is the default for both `apply_coverage` and
`apply_hazards`; pass `False` for legacy edge-precise behaviour.

Real Osaka 大阪市 numbers now visible across all 3 hazards:

| hazard | hit | % of 615,513 |
|---|---:|---:|
| `river_flood`   | 446,078 | 72 % |
| `tsunami`       | 202,679 | 33 % |
| `storm_surge`   | 326,894 | 53 % |

(`docs/osaka_flood.png`, `docs/osaka_tsunami.png`, `docs/osaka_stormsurge.png`)

### Real hazard intersection on 大阪市 + GDAL gotcha

- **451,429 of 615,513 Osaka buildings hit a river-flood想定** (73 %), 
  computed by the real `plateau hazard 27100` invocation against PLATEAU's
  prefecture-wide 河川洪水想定最大規模 polygons. Wall-clock: ~45 minutes
  on Apple M-series; hot path is `libgeos PreparedPolygon::intersects`.
- `docs/osaka_flood.png` — 615,513-building flood heatmap with 淀川 visible
  as the horizontal high-depth swath.
- **GDAL `OGR_GEOJSON_MAX_OBJ_SIZE=0`** set at import time in
  `sources/citygml.py`. PLATEAU's prefecture-wide tsunami / storm_surge
  layers ship as single ~200–400 MB GeoJSON features, exceeding the
  default 200 MB cap. Documented in `docs/PERFORMANCE.md`.

### Eight cities + `plateau hazard` + deck.gl demo

- **Bundled catalog now has 8 cities**, each with an admin polygon
  shipped: 渋谷区, 新宿区, 横浜市, 鎌倉市, 名古屋市, 大阪市, 福岡市, 札幌市.
  CKAN URLs verified live; admin polygons sourced from MLIT 国土数値情報 N03
  for non-Tokyo cities. `tests/test_admin.py` pins parity between catalog
  and bundled polygons.
- **`plateau hazard CITY`** subcommand — re-runs hazard intersection on an
  existing `buildings.parquet` without redoing Gate A. Designed for the
  `plateau build --no-hazards` → `plateau hazard` two-step on huge cities;
  reuses the cached hazard GeoJSON under `_work/hzd_*`.
- **deck.gl `Tile3DLayer` demo** at `examples/browser_deckgl/` —
  alternative browser stack to the three.js demo, same Gate B data
  contract. Both build clean against vite.

### Third city Osaka 大阪市 + perf + headless screenshot + bench

- **Osaka-shi 27100 (2024) builds end-to-end on real data**: 616,115 raw
  buildings, **615,513 clipped to 大阪市 admin** (single-row polygon
  dissolved from MLIT N03's 24 wards).
- `--no-hazards` CLI flag for fast Gate A on huge cities: Osaka has a 3.4 GB
  flood-polygon layer and >600k buildings, where the geos PreparedPolygon
  spatial join dominates wall-clock time. Skipping leaves all hazard fields
  NULL/unknown — honesty-preserving.
- **Hazard load uses pyogrio bbox prefilter** when an admin polygon is
  available — drops the Osaka river_flood load time from minutes-of-pure-IO
  to ~150 s by skipping prefecture-wide polygons that lie outside the city.
- Pipeline now **tolerates missing hazard subdirs**: Osaka has no `lsld`
  but ships `tnm` + `htd`. Catalog entry updated accordingly; gate_a logs
  and continues with `coverage_confidence: unknown` for missing kinds.

### Third city + headless browser screenshot + bench

- **Osaka-shi (27100) added** to the bundled catalog. Real CityGML URL from
  PLATEAU CKAN (2024 release). The pipeline now tolerates missing hazard
  subdirs (Osaka has no `lsld` but has `tnm` + `htd`) — gate_a logs a
  warning and continues with `coverage_confidence: "unknown"` for the
  missing kind. Catalog entry uses tsunami / storm_surge accordingly.
- **Admin polygon for Osaka** added to `data/japan_admin.geojson` (Tokyo +
  Osaka now). Sourced from MLIT 国土数値情報 N03 (2024 vintage) — 24 大阪市
  wards dissolved to a single 27100 polygon. Provenance recorded in
  `manifest.notes` (`Tokyo: © dataofjapan/land (MIT); Osaka & others: MLIT
  国土数値情報 N03 行政区域 (2024)`).
- **`plateau bench`** subcommand — DuckDB query suite (filter / decade
  histogram / hazard intersect / centroid / bbox / honesty-pivot) timed
  with median + p99 over N iterations. All six queries sub-2 ms median on
  real Shibuya 41,858-row parquet (Apple M-series).
- **Headless Chromium screenshot** of the colorBy demo running against
  real Gate B output → `docs/browser_demo.png`. Per-feature shading via
  custom `ShaderMaterial` keyed on `_FEATURE_ID_0` + 1D RGBA color
  texture; ramp = magma over measuredHeight 0–60 m.
- **Style tables now uncompressed** (apache-arrow JS doesn't implement
  compressed record-batch decoding); ~20 KB per tile so the size cost is
  negligible.
- **`ReorientationPlugin`** replaces deprecated `setLatLonToYUp` in the demo.
- 1 new test (`test_bench`).

### Multi-city + browser demo end-to-end

- **新宿区 (13104) also runs end-to-end on real data**: 57,485 clipped buildings,
  43,046 river-flood hits, 344 landslide-zone buildings.
- Side-by-side `docs/multicity.png` proves pipeline reproducibility.
- Browser `colorBy` demo verified live against real Gate B/C output:
  vite middleware serves every artifact (`tile_index.json`, `tileset.json`,
  per-zoom glb, encoded-name Arrow style tables) with correct MIME types,
  CORS `*`, and `Accept-Ranges: bytes`.
- Tippecanoe-enriched PMTiles: 11 fields per feature including all hazard
  scalars; bounds correctly clipped to the ward (13 MB vs 24 MB for the
  prefecture-wide nusamai fallback).
- Gate B now symlinks `out/3dtiles/` → `out/_work/bldg/3dtiles/` so the
  browser bundle has a stable public URL.
- 9 new tests (admin lookup, hazard rank parsing, Gate B/C handoff regression).

### End-to-end ABC ran on real data

`plateau build 13113 --gates ABC` produces every advertised artifact from a
real 636 MB PLATEAU bundle:

```
buildings.parquet         20 MB   41,858 buildings × 62 cols
buildings.pmtiles         24 MB   nusamai native sink (no tippecanoe)
buildings/13113.fgb       197 MB  full-precision FlatGeobuf
manifest.json + tile_index.json (875 entries)
style/                    875 × Arrow IPC per-tile attribute tables
_work/bldg/3dtiles/       2,285 glb files + tileset.json
```

Gate B mapping verified: **100% coverage** (41,858/41,858 buildings carry a
unique `(tile_content_uri, tile_feature_id)` joint key) across 875 unique
3D-Tiles content files.

### Fixed (this iteration)

- Gate C was overwriting Gate B's parquet with the pre-B gdf, silently
  dropping `tile_content_uri` / `tile_feature_id` columns. `GateBResult` now
  carries the enriched gdf and the CLI threads it into Gate C.

### Added (this iteration)

- `plateau_parquet.admin` — bundled Tokyo ward admin polygons (`tokyo_admin.geojson`,
  derived from `dataofjapan/land` MIT) with `load_admin(city_code)`.
- `plateau build --admin <path>` / `--no-admin` flags.
- Gate A now clips buildings to admin boundary by default (when the bundled
  city is recognised) — PLATEAU zips are prefecture-wide so unfiltered output
  carries ~2× the buildings the `city_code` implies.
- Real-flood depth extraction: `floodingRiskAttribute[].rank` Japanese labels
  (`"0.5m未満"`, `"0.5m以上3m未満"`, ...) mapped to meters; first
  validated end-to-end on Shibuya 2023 yielding **33,561 building flood hits**
  + **64 landslide-zone buildings**.
- Catalog `udx_subdir` field describing which subdirectory inside a PLATEAU
  bundle holds each theme's GML.

### Validated against real PLATEAU data

End-to-end Gate A run on **PLATEAU Shibuya 2023** (`13113_shibuya-ku_pref_2023_citygml_2_op.zip`,
636 MB CityGML, **90,299 buildings**) — produced a 44 MB GeoParquet + manifest +
`plateau verify` clean report. Discoveries fixed in this round:

- nusamai CLI binary name and flag set (`nusamai`, positional gml glob, `--sink geojson|3dtiles`, `-t use_lod=...`).
- Real PLATEAU bundle layout: one zip per (city, year) containing every theme under `udx/<theme>/`. Catalog entries gained a `udx_subdir` field.
- nusamai GeoJSON sink emits a **directory** (`<feature_class>.geojson` per type), not a single file. Per-kind feature names (`Building`, `WaterBody`, `SedimentDisasterProneArea`, `UseDistrict`) wired through.
- Codelist values resolve to **Japanese descriptions** (`住宅`, `木造`, `準耐火造`) — not codes. `STRUCTURE_MAP` / `USAGE_MAP` rewritten.
- Nested objects (`buildingDetailAttribute`) and arrays (`usage`) arrive as **JSON-stringified** values; normaliser uses `json.loads` per row.
- `measuredHeight = -9999` sentinel filtered in `normalise()`.
- Geometries are 3D (`MultiPolygon` with Z); `load_geojson` force-2Ds at read time.
- `convert_buildings` is now idempotent — second runs skip nusamai when the GeoJSON target is already present.
- Real PLATEAU Shibuya 2023 has 0% `yearOfConstruction` / `buildingStructureType` populated; `poster --color-by height` is the appropriate visual.

### Added
- `plateau verify` subcommand and `plateau_parquet.verify` module — schema /
  honesty / UID-uniqueness / manifest cross-consistency checks in one call.
- `plateau poster` subcommand — Building Age Rainbow PNG/SVG poster
  generator using matplotlib. Requires the `[poster]` extra.
- Real `EXT_structural_metadata` UTF-8 string property table unpacking in
  `ops/tiles3d.py`, including external-tileset recursion.
- `[all]` install extra.
- PyPI trusted-publishing release workflow.
- `CITATION.cff` for academic citability.
- End-to-end Gate A integration test using synthetic fixtures (no network /
  no converter required).

### Fixed
- `sources/citygml.py` now uses the real `nusamai` CLI shape:
  positional GML glob, `--sink geojson|3d-tiles`, `--output`, `-t use_lod=...`.
  GeoJSON and 3D Tiles are produced via two separate invocations because the
  CLI has no combined mode.
- `ops/attributes.py` now uses the real nusamai GeoJSON property keys:
  camelCase local names with no namespace prefix, `Code` as a bare string,
  nested `buildingDetailAttribute` array for structure / fireproof codes.
- Browser demo applies `setLatLonToYUp` so the scene sits at the city origin
  rather than at the centre of the Earth.
- `gate_b` survives empty `tile_feature_id` columns (was crashing on
  `astype("Int32")` of an inferred object dtype).


## [0.1.0] — 2026-05-22

Initial public release. Gate A / B / C all implemented; pilot cities 渋谷区
and 新宿区 in the bundled catalog.

### Added
- Gate A: `buildings.parquet` + `manifest.json` from PLATEAU CityGML, with
  full hazard coverage extent resolution (`explicit_polygon` /
  `declared_full_admin` / `unknown`) and multi-source `_depth_max`.
- Gate B: per-tile Arrow IPC style tables + `tile_index.json`, plus
  `(tile_content_uri, tile_feature_id)` mapping via `EXT_structural_metadata`.
- Gate C: `buildings.pmtiles` + per-ward FlatGeobuf + zoning_use / far_max
  backfill from 都市規劃 GML.
- `plateau` CLI (`build`, `info`, `cache`).
- Browser `colorBy` demo (three.js + 3d-tiles-renderer + apache-arrow).
- Catalog entries for **13113 Shibuya-ku** and **13104 Shinjuku-ku**, 2024.

### Honesty invariants enforced
- Hazard coverage extent is **never** reverse-engineered from inundation
  polygons. Tested in `tests/test_coverage_semantics.py`.
- `covered = false` means *unknown*, not *safe*.

### Out of scope (v1)
- Earthquake intensity (needs J-SHIS).
- Real-time data.
- Indoor LOD4.
- Cross-year canonical building ID (`canonical_building_id`).
