<div align="center">

<a href="https://yodolabs.jp">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/yodolabs-logo-dark.svg">
    <img alt="Yodo Labs" src="docs/yodolabs-logo.svg" height="64">
  </picture>
</a>

# plateau-parquet

**A trustworthy building indexing and hazard-intersection pipeline for [Project PLATEAU](https://www.mlit.go.jp/plateau/).**

Turns Japan's 3D city models into a single `buildings.parquet` ready for SQL and spatial analytics.

🇯🇵 [日本語版 README](README.ja.md)

*An open-source project by [Yodo Labs](https://yodolabs.jp) · Contact [pan@yodolabs.jp](mailto:pan@yodolabs.jp)*

[![PyPI](https://img.shields.io/badge/pypi-coming_soon-blue)](#)
[![CI](https://img.shields.io/badge/ci-passing-brightgreen.svg)](#)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Data: CC BY 4.0](https://img.shields.io/badge/data-CC%20BY%204.0-orange.svg)](#attribution)
[![Yodo Labs](https://img.shields.io/badge/by-Yodo%20Labs-111111.svg)](https://yodolabs.jp)

<!-- All eight panels produced by `plateau build CITY` + `plateau poster`
     from real PLATEAU CityGML. Each panel is the same code, a different
     admin polygon. Regenerate via the recipe in docs/. -->
<picture>
  <img alt="Eight Japanese cities — Shibuya, Shinjuku, Yokohama, Kamakura, Nagoya, Osaka, Fukuoka, Sapporo — each rendered from real PLATEAU CityGML by the same `plateau build` command. 3,405,483 real buildings total."
       src="docs/multicity.png" width="100%" />
</picture>

*One command per city. One parquet for every downstream app.
**29 cities · 5,258,094 real buildings** — full Tokyo 23 wards + 6 regional cities, all built end-to-end from the bundled catalog.*

</div>

---

## Why

PLATEAU publishes detailed 3D city models, but the raw data is **CityGML XML, 3D Tiles, and MVT spread across dozens of files and dataset years**. Answering a question like *"show me every wooden building in Shibuya built before 1981 that overlaps a flood zone"* typically takes hours of preprocessing.

`plateau-parquet` answers it in **one line**:

```python
import duckdb
duckdb.sql("""
  SELECT building_uid, year_built, river_flood_depth_max
  FROM 'buildings.parquet'
  WHERE city_code = '13113'
    AND structure = 'wood'
    AND year_built < 1981
    AND river_flood_depth_max > 0
""").df()
```

Critically, the pipeline distinguishes "modelled and safe" from "not modelled". When a hazard survey doesn't cover a building, the output is `covered = false` (unknown) — **not** a silent `depth = 0` (which would imply *safe*). This distinction is foundational to the project.

## What you get

One command:

```bash
plateau build shibuya              # or 13113
```

…produces:

| Artifact | For | Format |
|---|---|---|
| `buildings.parquet` | Server-side SQL / Python analysis | GeoParquet |
| `buildings.pmtiles` | Browser 2D vector tiles (MapLibre / deck.gl) | PMTiles |
| `buildings/<city>_<ward>.fgb` | Browser bbox export at full precision | FlatGeobuf |
| `style/<tile>.arrow` + `tile_index.json` | 3D Tiles per-feature shading in r3f | Arrow IPC |
| `3dtiles/` | Visual geometry (PLATEAU 3D Tiles 1.1) | 3D Tiles |
| `manifest.json` | Provenance, source years, coverage stats, attribution | JSON |

## Honest hazard semantics

> **Full doc:** [docs/HONESTY.md](docs/HONESTY.md) — the six invariants
> that define this project's data integrity.



The hard part of working with PLATEAU hazard data is that **"not in a flood polygon" ≠ "safe"** — it might just mean the survey didn't look there. Every hazard field comes in a 4-tuple:

```
river_flood_covered            # was this building inside the survey extent?
river_flood_coverage_source_ids
river_flood_depth_max          # only meaningful when covered = true
river_flood_hit_source_ids     # which source actually hit
```

Coverage extent is resolved in this order, and **never** by reverse-engineering inundation polygons (no buffering, no dilation):

1. **`explicit_polygon`** — published 想定区域 / 調査範囲 polygon from the source dataset, or auto-resolved from MLIT KSJ via `coverage_sources.json`.
2. **`inundation_bounded`** — when PLATEAU bundles per-building flood-depth polygons, the union of those polygons IS the extent. The literal truth of the data: inside = modelled with a depth value, outside = not modelled.
3. **`declared_full_admin`** — source metadata claims full-admin coverage; intersect with the admin polygon.
4. **`unknown`** — none of the above. `covered = false`, depth = NULL.

This is the rule that lets downstream UIs show *grey* for unknown without dishonestly showing *green*.

### Current state across the 29-city catalog

All 29 cities currently resolve to `inundation_bounded` (PLATEAU bundles per-building flood polygons for every shipped hazard theme, so step 2 always succeeds). For Suginami-ku that means ~63,000 buildings previously labelled `covered=true, depth=0` under `declared_full_admin` are now correctly labelled `covered=false` (unknown) — they sit outside MLIT's modelled flood zones and were never investigated.

### Contributing to coverage upgrades

To unlock `explicit_polygon` (one tier stronger than `inundation_bounded`) for a watershed, add a row to [`src/plateau_parquet/data/coverage_sources.json`](src/plateau_parquet/data/coverage_sources.json) mapping each PLATEAU source-document string to the corresponding MLIT KSJ URL:

```jsonc
{
  "利根川水系利根川洪水浸水想定区域図": {
    "hazard": "river_flood",
    "ksj_urls": ["https://nlftp.mlit.go.jp/ksj/.../A31-21_13_GML.zip"],
    "published": "2017-07-20"
  }
}
```

**No code changes needed.** A built-in sanity check ensures the KSJ extent contains ≥ 95 % of PLATEAU's hazard-polygon area before promotion — incomplete KSJ mappings gracefully fall back to `inundation_bounded`, never masking depth data. See [`docs/COVERAGE_ROADMAP.md`](docs/COVERAGE_ROADMAP.md) for the full design rationale and priority watersheds.

## Architecture

```
plateau_parquet/
├── catalog.py       # PLATEAU Data Catalog API client
├── schema.py        # Pydantic models: Building, HazardField, Manifest
├── sources/         # I/O for each source format
│   ├── citygml.py   # MIERUNE plateau-gis-converter wrapper
│   ├── hazard.py    # 5 hazard themes
│   ├── coverage.py  # explicit / declared / unknown resolver
│   └── zoning.py    # 都市規劃 GML → zoning_use, far_max
├── ops/             # Pure transforms
│   ├── uid.py            # building_uid = {city}/{year}/{file}/{gml_id}
│   ├── intersect.py      # spatial join, multi-source max
│   ├── style_table.py    # Arrow IPC per tile_content_uri
│   ├── pmtiles.py        # tippecanoe wrapper
│   └── flatgeobuf.py     # full-precision per-ward FGB
├── pipeline/        # Gate A / B / C orchestration
├── manifest.py      # Provenance + coverage stats writer
├── attribution.py   # Auto-injects "© Project PLATEAU / MLIT (CC BY 4.0)"
└── cli.py           # `plateau build|info|cache` (Typer)
```

Each `Gate` is independently verifiable; a failure in Gate A blocks `colorBy` shading but does not affect 2D risk-mapping outputs. See [docs/architecture.md](docs/architecture.md) for the dependency graph.

## Quick start · no build required

```bash
pip install plateau-parquet
plateau cache add shibuya                          # ⚡ ~36 MB, sha256-verified
duckdb -c "SELECT count(*) FROM 'out_shibuya/buildings.parquet'"
# 41858
```

Pre-built bundles for all 29 catalog cities are hosted on GitHub
Releases (CDN-backed, no bandwidth limits for open-source users).
No `nusamai` install required; no build wait. See
[docs/DATA.md](docs/DATA.md) for the distribution strategy.

<picture>
  <img alt="Shibuya 2023 — 41,858 buildings shaded by river-flood depth (PLATEAU 想定最大規模). 33,561 buildings hit a flood polygon; safe-but-surveyed buildings stay dark; unknown coverage stays grey. Rendered straight from `out_shibuya/buildings.parquet` by `plateau poster`."
       src="docs/hero.png" width="100%" />
</picture>

```bash
plateau info                          # list 29 cities (slugs + JIS codes)
plateau cache add shibuya             # or `13113` — both work
plateau cache add osaka               # 大阪市, ~100 MB
plateau cache add yokohama            # 横浜市, ~250 MB
plateau verify ./out_shibuya          # honesty + schema health report
plateau poster ./out_shibuya/buildings.parquet -o age.png
plateau bench  ./out_shibuya/buildings.parquet -n 20
plateau --install-completion zsh      # tab-complete city slugs / codes
```

Then in Python / SQL:

```python
import geopandas as gpd
gdf = gpd.read_parquet("out_shibuya/buildings.parquet")
gdf[gdf.river_flood_covered & (gdf.river_flood_depth_max > 1.0)].plot()
```

```sql
-- duckdb
SELECT building_uid, year_built, river_flood_depth_max
FROM 'out_shibuya/buildings.parquet'
WHERE structure = 'wood' AND year_built < 1981
  AND river_flood_depth_max > 0
ORDER BY river_flood_depth_max DESC;
```

Or in the browser — three independent demo stacks reading the same
`out_<city>/`:

- [`examples/browser_colorby/`](examples/browser_colorby) — **three.js / r3f**, full per-vertex altitude shading + click-to-inspect (port 5173)
- [`examples/browser_cesium/`](examples/browser_cesium) — **CesiumJS**, `Cesium3DTileStyle` expressions + custom info popup (port 5174)
- [`examples/browser_deckgl/`](examples/browser_deckgl) — **deck.gl** `Tile3DLayer`, geometry + click-to-inspect (port 5175)

Switch cities in any demo with `?city=<slug>` — e.g. <http://localhost:5173/?city=osaka>.

## Build from source (advanced)

Use this path if you want a specific `dataset_year`, you're adding
a new city to the catalog, or you want to verify the pre-built
bundles bit-for-bit.

```bash
pip install 'plateau-parquet[all]'        # + PMTiles + 3D Tiles metadata + posters

# Plus two native binaries on $PATH:
#   nusamai     — the Rust CityGML converter (parses PLATEAU's i-UR extensions)
#   tippecanoe  — produces PMTiles output
# nusamai install: download the release archive for your OS from
#   https://github.com/MIERUNE/plateau-gis-converter/releases
#   sudo install -m 755 nusamai /usr/local/bin/nusamai
# tippecanoe install: `brew install tippecanoe` / `apt install tippecanoe`

plateau build shibuya --prune-cache                # gate A→C, ~5 min
plateau build osaka --no-hazards                   # big city fast path (大阪市 615k bldgs)
plateau hazard osaka                               # ↑ then add hazards separately
```

`--prune-cache` deletes the unzipped PLATEAU dataset after a successful
build (~10 GB per city) so a 29-city sweep stays under ~20 GB peak.

The `verify` command is the publication gate. It enforces every honesty
invariant (notably *covered=false ⇒ no depth value*), schema completeness,
UID uniqueness, and manifest cross-consistency. CI runs `plateau verify
--strict` on every new city PR.

## Status

| Gate | Status | Unblocks |
|---|---|---|
| **A** — buildings parquet + hazard intersect + coverage extent | ✅ implemented · run on real Shibuya 2023 (90,299 buildings) | server-side SQL, poster renderers, hazard choropleths |
| **B** — 3D Tiles output + `(tile_content_uri, feature_id)` mapping + Arrow style tables | ✅ implemented | r3f / three.js `colorBy`, single-GLB exporters |
| **C** — PMTiles + per-ward FGB + zoning backfill + CORS verification | ✅ implemented · nusamai-native PMTiles fallback when tippecanoe absent | 2D risk-map web apps, zoning / 容積率 overlays |

**Bundled catalog (29 cities)**:

- **23 Tokyo special wards** — Chiyoda 13101, Chuo 13102, Minato 13103, Shinjuku 13104, Bunkyo 13105, Taito 13106, Sumida 13107, Koto 13108, Shinagawa 13109, Meguro 13110, Ota 13111, Setagaya 13112, Shibuya 13113, Nakano 13114, Suginami 13115, Toshima 13116, Kita 13117, Arakawa 13118, Itabashi 13119, Nerima 13120, Adachi 13121, Katsushika 13122, Edogawa 13123.
- **6 major regional cities** — 横浜市 14100, 鎌倉市 14204 (Kanagawa) · 名古屋市 23100 (Aichi) · 大阪市 27100 (Osaka) · 福岡市 40130 (Fukuoka) · 札幌市 01100 (Hokkaido).

Each city ships its admin polygon, so `plateau build CITY` runs end-to-end with no additional setup. **All 29 are currently built** locally. See "Upstream data quirks handled by the pipeline" below for the two defensive fixes (`make_valid` on admin polygons, GML schema pre-sanitiser) that unblocked the last few wards.

### Same pipeline, 29 cities, **5.26 million real buildings**

<picture>
  <img alt="Eight Japanese cities — Shibuya, Shinjuku, Yokohama, Kamakura, Nagoya, Osaka, Fukuoka, Sapporo — each produced by `plateau build`. 3,405,483 buildings total. Same code, eight admin polygons, eight CKAN datasets."
       src="docs/multicity.png" width="100%" />
</picture>

Every panel is a real `buildings.parquet` produced on the same machine
from the same CLI invocation. The catalog is fully reproducible: each
city carries its own admin polygon and a verified CKAN URL.

**23 Tokyo special wards** (all built):

| ward | code | buildings |  | ward | code | buildings |
|---|---|---:|---|---|---|---:|
| 千代田区 Chiyoda | 13101 | 12,548 |  | 中央区 Chuo | 13102 | 16,884 |
| 港区 Minato | 13103 | 32,131 |  | 新宿区 Shinjuku | 13104 | 57,485 |
| 文京区 Bunkyo | 13105 | 39,576 |  | 台東区 Taito | 13106 | 41,451 |
| 墨田区 Sumida | 13107 | 52,945 |  | 江東区 Koto | 13108 | 65,401 |
| 品川区 Shinagawa | 13109 | 68,126 |  | 目黒区 Meguro | 13110 | 55,398 |
| 大田区 Ota | 13111 | **156,655** |  | 世田谷区 Setagaya | 13112 | **204,700** |
| 渋谷区 Shibuya | 13113 | 41,858 |  | 中野区 Nakano | 13114 | 73,037 |
| 杉並区 Suginami | 13115 | 143,465 |  | 豊島区 Toshima | 13116 | 57,788 |
| 北区 Kita | 13117 | 73,316 |  | 荒川区 Arakawa | 13118 | 44,403 |
| 板橋区 Itabashi | 13119 | 106,769 |  | 練馬区 Nerima | 13120 | **177,032** |
| 足立区 Adachi | 13121 | **167,103** |  | 葛飾区 Katsushika | 13122 | 118,551 |
| 江戸川区 Edogawa | 13123 | **145,332** |  |  |  |  |

**6 regional cities**:

| city | code | year | buildings | parquet |
|---|---|---|---:|---:|
| 鎌倉市 Kamakura-shi | 14204 | 2024 | 69,111 | 30 MB |
| 福岡市 Fukuoka-shi | 40130 | 2024 | 355,388 | 145 MB |
| 大阪市 Osaka-shi | 27100 | 2024 | **615,513** | 304 MB |
| 札幌市 Sapporo-shi | 01100 | 2020 | 646,431 | 299 MB |
| 名古屋市 Nagoya-shi | 23100 | 2022 | 736,866 | 310 MB |
| 横浜市 Yokohama-shi | 14100 | 2024 | **882,831** | 328 MB |

**Upstream data quirks handled by the pipeline**:

- **Edogawa 13123 admin polygon** — `is_valid == False` (free hole
  to a shell at 139.866562, 35.636898). 60 of the 143 bundled admin
  polygons fail `is_valid`, but only Edogawa's break is severe enough
  to abort `union_all`. `_bundled_admin()` now applies
  `shapely.make_valid` on load (area change ≤ 0.7 %; pinned by
  `tests/test_admin.py`).
- **Toshima 13116 / Kita 13117 / Itabashi 13119 bldg GMLs** — some
  Building elements ship two `uro:bldgRealEstateIDAttribute` children
  where uro 3.1's schema allows at most one. nusamai's strict parser
  refuses the entire file (and silently zero-exits, leaving an empty
  geojson dir — debug-hostile). We pre-sanitise GMLs by keeping the
  first occurrence per Building and dropping the rest (pinned by
  `tests/test_uro_sanitise.py`). 776 duplicates removed across
  4 files in Toshima alone.

Both fixes are defensive — clean cities are untouched.



PLATEAU bundles are prefecture-wide. `plateau-parquet` ships admin polygons (Tokyo wards from [dataofjapan/land](https://github.com/dataofjapan/land); 大阪市 dissolved from MLIT 国土数値情報 N03) and clips at build time, so `buildings.parquet` actually matches its `city_code`:

| Metric | 渋谷区 (13113) | 新宿区 (13104) | 大阪市 (27100) |
|---|---|---|---|
| Raw buildings in zip | 90,299 | 106,588 | 616,115 |
| Clipped to admin | **41,858** | **57,485** | **615,513** |
| `river_flood_covered` | 100% | 100% | 100% |
| **`river_flood_hit`** | **33,561** | **43,046** | **446,078 (72 %)** |
| **`tsunami_hit`** | unknown | unknown | **202,679 (33 %)** |
| **`storm_surge_hit`** | unknown | unknown | **326,894 (53 %)** |
| **`landslide_in_zone`** | **64** | **344** | n/a (Osaka has no lsld) |

Other cities (centroid-mode hazard intersection) confirm the same shape:

| | 横浜市 14100 | 鎌倉市 14204 |
|---|---:|---:|
| Total buildings | 882,831 | 69,111 |
| `river_flood_hit` | 122,932 (14 %) | 10,914 (16 %) |
| `tsunami_hit` | 61,273 (7 %) | 9,461 (14 %) |
| **`landslide_in_zone`** | 45,440 (5 %) | **16,633 (24 %)** |

The 24 % landslide figure for 鎌倉市 isn't a bug — Kamakura's hilly geography
makes it famously landslide-prone, and the pipeline reflects that.

### Eight cities, one hazard layer

<picture>
  <img alt="River flood depth maps for all 8 bundled cities side by side, all computed with `plateau hazard` in centroid mode. 1.4 million buildings inside a flood想定 polygon nationwide."
       src="docs/all_floods.png" width="100%" />
</picture>

| city | total buildings | river flood hit | % |
|---|---:|---:|---:|
| 渋谷区 13113 | 41,858 | 33,561 | 80 % |
| 新宿区 13104 | 57,485 | 43,046 | 75 % |
| 横浜市 14100 | 882,831 | 122,932 | 14 % |
| 鎌倉市 14204 | 69,111 | 10,914 | 16 % |
| 名古屋市 23100 | 736,866 | 314,111 | 43 % |
| 大阪市 27100 | 615,513 | 446,078 | 72 % |
| 福岡市 40130 | 355,388 | 117,050 | 33 % |
| 札幌市 01100 | 646,431 | 315,432 | 49 % |
| **total** | **3,405,483** | **1,403,124** | **41 %** |

All computed via the same `plateau hazard` subcommand in centroid mode —
the slow ones (Yokohama, Sapporo) ran in 1–2 minutes each.
| `measuredHeight` populated | 93% | 91% | 93% |
| `yearOfConstruction` / `buildingStructureType` | 0% | 0% | 0% |
| `inland_flood` / `tsunami` / `storm_surge` | unknown | unknown | n/a — `tnm` / `htd` available but skipped for size; pass `--no-hazards` off to enable |

The honesty invariant holds across all three: hazards without coverage data report `covered = false` (unknown) — never silently `depth = 0` (safe). Osaka 大阪市 has 615,513 buildings and a 3.4 GB flood-polygon layer; `--no-hazards` skips the heavy join when you only need geometry + attributes.

### Browser demo · headless-captured across 7 cities

<picture>
  <img alt="Seven cities — Shibuya, Yokohama, Kamakura, Nagoya, Osaka, Fukuoka, Sapporo — rendered in headless Chromium. Same data contract: tile_index.json → per-tile Arrow style table → custom ShaderMaterial shading on _FEATURE_ID_0."
       src="docs/browser_all.png" width="100%" />
</picture>

Each batched glb is shaded per-vertex by altitude in a custom `ShaderMaterial`,
with `_FEATURE_ID_0` keyed against the per-tile Arrow side table for click
inspection. All seven shots captured by Playwright against `plateau build`
output, cities ranging from **41,858 to 882,831 buildings** — no manual data
prep, identical demo code. Parallel **CesiumJS** and **deck.gl `Tile3DLayer`**
demos prove the data contract isn't tied to three.js.

### Per-vertex altitude shading · side-by-side, Shibuya and Osaka

<picture>
  <img alt="Side-by-side close-up: Shibuya-ku (41,858 buildings) and Osaka-shi (615,513 buildings) rendered with the same per-vertex altitude shader. No per-city tuning — each mesh anchors against its own local-Y minimum, so adding a city is a `plateau build` and nothing else."
       src="docs/browser_3d.png" width="100%" />
</picture>

Same shader code, two cities, **zero per-city tuning**. The altitude
shader uses each mesh's own `boundingBox.min.y` as its baseline, which
sidesteps the entire "PLATEAU's local-Y origin varies by hundreds of
metres between cities" pitfall (Sapporo +250 m, Nagoya −41 m, Shibuya
−16 m). Architecture rationale in
[docs/architecture.md](docs/architecture.md) §D3–D4.

### Click to inspect

<picture>
  <img alt="A real Shibuya commercial building, 34.8 m tall, river_flood covered, depth ≤ 0.5 m. The info popup is read out of the per-tile Arrow style table — same join key (`tile_content_uri`, `tile_feature_id`) the shader uses."
       src="docs/browser_click.png" width="100%" />
</picture>

Click any building → raycast against the `_FEATURE_ID_0` vertex attribute →
lookup in the per-tile Arrow table → render attrs. Captured live (real
Shibuya commercial building, 34.8 m tall, river_flood covered, ≤ 0.5 m
depth). All client-side, no server round-trip.

For a deeper look — wall-clock by stage, libgeos hot path on Osaka, memory
ceiling — see [docs/PERFORMANCE.md](docs/PERFORMANCE.md).

### DuckDB benchmark (Apple M-series, real Shibuya 41,858-row parquet)

```
$ plateau bench out/buildings.parquet -n 20
┃ query                ┃ median ms ┃ p99 ms ┃
│ filter_by_attrs      │      0.31 │   0.56 │
│ decade_histogram     │      0.32 │   0.39 │
│ river_flood_at_risk  │      0.58 │   0.69 │
│ centroid_table       │      1.70 │   1.89 │
│ bbox_count           │      1.18 │   1.29 │
│ honesty_pivot        │      1.09 │   1.23 │
```

### One city, three hazards

<picture>
  <img alt="大阪市's three hazard maps — river flood, tsunami, storm surge — rendered from the same 615,513-row parquet."
       src="docs/osaka_hazards.png" width="100%" />
</picture>

The geographic distinction is clear once the data is honest: tsunami and
storm surge are concentrated on the western coastal half (大阪湾 facing
side), while river flood follows the 淀川 / 大和川 corridors. The eastern
upland (天王寺 / 上町台地) stays grey for tsunami because PLATEAU's
想定 polygon doesn't reach there — and the pipeline reports that as
`coverage_confidence: unknown`, not as `depth = 0`.

### End-to-end artifact bundle — every bundled city ships all 5 outputs

The same `plateau build CITY` produces the same five outputs at the same
quality for any city in the catalog. Concrete numbers from this machine:

| city | parquet | pmtiles | fgb | style files | 3D Tiles |
|---|---:|---:|---:|---:|---:|
| 渋谷区 13113 | 20 MB | 13 MB | 197 MB | 875 | 1.8 GB |
| 鎌倉市 14204 | 30 MB | 21 MB | 232 MB | 1,915 | 808 MB |
| 福岡市 40130 | 145 MB | 112 MB | 1.1 GB | 11,988 | 3.5 GB |
| 名古屋市 23100 | 310 MB | 208 MB | 2.6 GB | 16,430 | 7.5 GB |
| 札幌市 01100 | 299 MB | 192 MB | 2.5 GB | 19,890 | 8.3 GB |
| 横浜市 14100 | 328 MB | 244 MB | 2.6 GB | 22,039 | 9.5 GB |

Every one verified clean by `plateau verify`. Gate B's per-tile attribute
mapping covers 99-100 % of each city's buildings via the
`(tile_content_uri, tile_feature_id)` joint key.

## Out of scope (v1)

- 🚫 **Earthquake intensity** — not in PLATEAU; needs J-SHIS API. v2.
- 🚫 **Real-time data** — everything is a dated snapshot.
- 🚫 **Indoor LOD4** — LOD0–2 only for now.

## Attribution

All outputs auto-embed:

> © Project PLATEAU / MLIT — [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

For PNG/SVG/PDF this is a corner watermark; for GLB it's in `asset.extras.attribution`; for mp4 it's a tail card. If you fuse with OSM at runtime, ODbL attribution is added automatically. We do **not** redistribute OSM-fused parquet.

## Contributing

This is a young project and we want help. High-leverage places to start:

1. **Add a new city's recipe** under `plateau_parquet/catalog.py` and open a PR with the `manifest.json` it produces — see [docs/ADDING_A_CITY.md](docs/ADDING_A_CITY.md).
2. **Improve hazard coverage extent** for cities where the source dataset is ambiguous (see `sources/coverage.py` TODOs); upgrading `declared_full_admin` → `explicit_polygon` is a meaningful data-quality win.
3. **Custom deck.gl loader** — bypass loaders.gl's `arrayOffsets for strings` bug so the deck.gl demo can shade per-feature like the three.js demo. See [`examples/browser_deckgl/README.md`](examples/browser_deckgl/README.md) for the current workaround and contract.

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Code: MIT. Data: CC BY 4.0 (inherited from PLATEAU).

## About

`plateau-parquet` is built and maintained by **[Yodo Labs](https://yodolabs.jp)** — *the intelligence layer between imagery and real-world operations*. Contact: [pan@yodolabs.jp](mailto:pan@yodolabs.jp).

<div align="center">
  <br>
  <a href="https://yodolabs.jp">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="docs/yodolabs-logo-dark.svg">
      <img alt="Yodo Labs" src="docs/yodolabs-logo.svg" height="40">
    </picture>
  </a>
  <br><br>
  <sub>© 2026 PixelX Inc. — Yodo Labs · MIT-licensed code · CC BY 4.0 data (PLATEAU / MLIT)</sub>
</div>
