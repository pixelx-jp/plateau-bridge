# Architecture

plateau-parquet is organised as **layers**, not modules-by-feature. Each layer has
one job and depends only on layers below it.

```
┌─────────────────────────────────────────────────────────────┐
│  cli.py             (Typer entrypoint — `plateau ...`)      │
├─────────────────────────────────────────────────────────────┤
│  pipeline/          (Gate A / B / C orchestrators)          │
├─────────────────────────────────────────────────────────────┤
│  ops/               (Pure transforms, no I/O)               │
│  ├─ uid.py          building_uid                            │
│  ├─ attributes.py   CityGML codelist → enums                │
│  ├─ intersect.py    coverage + hit spatial joins            │
│  ├─ style_table.py  per-tile Arrow IPC                      │
│  ├─ tiles3d.py      tileset.json walking                    │
│  ├─ pmtiles.py      tippecanoe wrapper                      │
│  └─ flatgeobuf.py   per-ward FGB writer                     │
├─────────────────────────────────────────────────────────────┤
│  sources/           (I/O — one module per source format)    │
│  ├─ download.py     httpx + unzip + cache                   │
│  ├─ citygml.py      MIERUNE converter shell-out             │
│  ├─ hazard.py       5 hazard themes                         │
│  ├─ coverage.py     extent resolver (explicit/declared/None)│
│  └─ zoning.py       都市規劃 GML                            │
├─────────────────────────────────────────────────────────────┤
│  schema.py          (Pydantic + Arrow source of truth)      │
│  catalog.py         (Static registry of city/year datasets) │
│  verify.py          (Post-build health checks)              │
│  poster.py          (Building Age Rainbow renderer)         │
│  config.py / cache.py / manifest.py / attribution.py        │
└─────────────────────────────────────────────────────────────┘
```

## Hard rules

1. **`ops/` is pure.** No filesystem, no HTTP, no subprocess. This is the only
   rule we test with `mypy --strict`.
2. **`sources/coverage.py` never reverse-engineers extent from inundation polygons.**
   See `tests/test_coverage_semantics.py`.
3. **`building_uid` is versioned by dataset_year.** A canonical, cross-year ID
   belongs in v2 and lives next to (not replacing) `building_uid`.

## Gate dependency graph

> For the full end-to-end Mermaid sequence diagram across all stages
> (download → nusamai → clip → normalise → coverage → hazards → tiles →
> style tables → PMTiles → FGB → manifest), see [SEQUENCE.md](SEQUENCE.md).


```
Gate A ─── parquet ──→ downstream poster tools (Building Age, hazard choropleth, etc.)
   │                   Python / DuckDB / GeoPandas analysis
   │
   ├─→ Gate B ── style/*.arrow + tile_index ──→ r3f / three.js colorBy
   │                                            3D Tiles MCP / GLB exporters
   │
   └─→ Gate C ── PMTiles + FGB + zoning ──→ 2D risk-map web apps
                                            zoning / 容積率 visualisations
                                            MapLibre / deck.gl overlays
```

A failed Gate **never blocks an independent gate's consumers**; e.g. if Gate B
verification fails, r3f falls back to LOD1 and the 2D risk-map apps (Gate C)
ship unaffected.

## Why Arrow IPC for style tables and not PMTiles?

PMTiles is a *spatial-tile index*. When the 3D-Tiles renderer is shading a
batch from a `.glb`, it has only `(tile_content_uri, feature_id)` — no spatial
query handle. Forcing a PMTiles round-trip on every draw call is the wrong
shape. Tile-scoped Arrow IPC files are the right shape: load one file per
loaded tile, do an O(1) lookup by feature_id, done.

## Why so many catalog files?

The bundled `catalog_registry.json` is the **only** place that knows dataset
URLs. Adding a new city is a single-file PR. CI generates a `manifest.json`
diff and we ship it.

---

# Decisions, with reasoning

The rest of this doc captures load-bearing decisions made in the project's
v0.1 push — ones that were arrived at non-obviously and that a future
contributor (or future-you) is likely to want to reverse without knowing
the constraint. Each subsection answers: **what's the choice, what
alternative was on the table, what made us reject it.**

## D1 — Height column has provenance, not just a value

`buildings.parquet` carries `height` (Float32) AND `height_source`
(enum: `measured | geometric | floors_estimated | footprint_fallback | unknown`).
Analysts can `WHERE height_source IN ('measured', 'geometric')` if they
want strict-only.

**Alternative**: `height` is the cleaned `measuredHeight` only;
NULL when missing. Forces every consumer to special-case NULL and
costs us colourful demos.

**Rejected because**: PLATEAU's `measuredHeight` is LiDAR-derived and
sentinel-NULL for 5–10 % of buildings. Per-building fallback chain
(geometric → floor-estimate → footprint default) keeps `height`
ALWAYS-populated for downstream rendering AND preserves honesty via
the `height_source` column. See `tests/test_geometric_height.py`
and `src/plateau_parquet/ops/geometric_height.py` for the geometric
back-fill mechanics.

## D2 — Geometric height extraction must respect interleaved bufferViews

`src/plateau_parquet/ops/geometric_height.py` reads vertex
`POSITION + _FEATURE_ID_0` attributes honouring `bufferView.byteStride`
and `accessor.byteOffset`. nusamai writes both attributes into one
interleaved bufferView with `byteStride = 36` bytes per vertex.

**Alternative**: read accessor bytes as a packed array.

**Rejected because**: packed reads give garbage values that *look*
plausible (float32 of mis-aligned position bytes can yield numbers in
a believable range). Test caught this once — kept that synthetic-GLB
test (`tests/test_geometric_height.py`) in place to prevent regression.

## D3 — Demos colour by **per-vertex altitude**, not per-building height

The three.js shader uses `position.y - mesh.boundingBox.min.y` as the
height signal. The shader does not read `_FEATURE_ID_0` for colouring
(it does for click-highlight, but the base colour ramp has no concept
of features).

**Alternative considered**: per-feature colour texture indexed by
`_FEATURE_ID_0`, built from a per-tile Arrow side-table. This was the
original v0.1 approach.

**Rejected because**: PLATEAU's `bldg:Building` features are
**administrative units** (one parcel, one feature). A single visual
building mass is regularly split into 2–12 features with very different
`measuredHeight` values (Shibuya 2023 has a real case of a 9 m podium
attached to a 243 m tower as two features). Any per-feature shading
exposes those administrative seams as colour cliffs. The cliffs cannot
be removed by "merge touching footprints" heuristics: small ε misses
the cases, large ε bridges alleys.

Per-vertex altitude shading eliminates the entire class of bug **by
construction** — the shader can't produce a colour cliff at an
admin boundary because it doesn't know admin boundaries exist.

Trade-off accepted: lose "this building IS 60 m tall, that one IS 30 m
tall" as a flat-colour per-building signal. Compensation: each
building's gradient encodes its height (the *taller* the building, the
*more colour range* it spans), and the click-popup still shows the
per-feature `measuredHeight` for analysts who want the number.

`building_complex_uid` + `complex_max_height` columns in the parquet
are kept (they were the heuristic-merge approach) so SQL analysts can
still `GROUP BY complex_uid` — but the demos don't read them.

## D4 — Altitude shader uses **per-mesh local baseline**, not city constants

Each mesh material's `uYBase` uniform is set at load time from that
mesh's own `boundingBox.min.y`. Zero per-city configuration. Adding
a new city is `plateau build` and nothing else.

**Alternatives considered**:

1. **Hardcoded per-city `yBase`** in `cities.ts`. Worked technically.
   Rejected because adding a city becomes "build it + sample 40 GLBs
   to compute the baseline + add a row to cities.ts" — a manual audit
   step that doesn't scale.
2. **World-Y after `modelMatrix`**. Would naturally normalise across
   cities IF the renderer's world frame were "metres above ground".
   Rejected because 3d-tiles-renderer leaves world coordinates in
   ECEF after `ReorientationPlugin` — `worldPos.y` is in millions
   (Earth-radius scale), not altitude. Verified by injecting a debug
   uniform on three cities.

**Trade-off of per-mesh baseline**: a tile spanning sloped terrain
anchors all its vertices to the tile's lowest corner, making
higher-elevation buildings look slightly taller than they truly are.
Acceptable for visual demos (PLATEAU tile size ≤ 5 km, most Japanese
city cores are flat enough). For accurate altitude analysis,
downstream tools should compute per-building altitude directly from
`buildings.parquet`'s geometry column, not from the demo shader.

## D5 — deck.gl demo runs in geometry-only mode behind a documented banner

`examples/browser_deckgl/src/main.ts` passes
`loadOptions.gltf.excludeExtensions = { EXT_structural_metadata: false, EXT_mesh_features: false }`
to bypass loaders.gl 4.x's
"Not implemented - arrayOffsets for strings" crash on PLATEAU's
variable-length `gml_id` property table. The demo's legend
acknowledges this limitation.

**Alternative**: write a custom Tiles3DLoader that skips the broken
property-table path but exposes feature IDs.

**Deferred, not rejected**. The current banner is honest enough for a
demo; a full custom loader is a separate ~2–3 h project tracked in
[CONTRIBUTING.md](../CONTRIBUTING.md). Worth doing for open-source
polish, not blocking for v1 ship.

## D6 — `tile_index.json` is exhaustive over every URI in `tileset.json`

Even tiles whose buildings were entirely admin-clipped get an empty
Arrow side-table emitted by `gate_b`. The map from `tile_content_uri`
to `style/<encoded>.arrow` is total.

**Alternative**: emit only tiles that have at least one row.

**Rejected because**: when a renderer streams in an "empty" tile and
looks it up, `tileIndex[uri] === undefined` is indistinguishable from
"index is stale" or "we forgot this tile". The renderer falls back to
the glTF default material, which is jarringly different from the
shader's "unknown" colour, producing the sharp diagonal cliffs reported
by users mid-development. Always-present index → always-applied shader
→ consistent visual.

## D7 — Building-complex grouping is OFFLINE only

`src/plateau_parquet/ops/building_complex.py` runs in gate_b and writes
`complex_uid` / `complex_max_height` columns to the parquet.
The demos do not use these columns for shading (see D3).

**Why we kept it despite the demos abandoning it**: SQL analysts on
the parquet want to ask "what's the tallest building in this complex"
or "give me all buildings on the Shibuya Hikarie plot". Group-by
queries on `complex_uid` answer those without needing geometric
joins. The compute happens once at build time (~30 s per city).

## D8 — Default touch epsilon is 0.5 m for building complexes

`compute_complexes(eps_m=0.5)` is the default — buildings within 0.5 m
of each other fuse into one complex.

**Why 0.5 m**: PLATEAU's positional accuracy is ~30 cm; construction
modelling routinely leaves 20–40 cm "ghost gaps" between physically-
joined buildings. 0.5 m absorbs those without bridging real alleys
(Tokyo's narrowest are ~1 m). Tested at 0.1 m (too tight; users
reported visible split tower/podium cases) and 1.0 m (too loose;
visibly merges across alleys).

## D9 — Per-city data routing via URL prefix, not env vars

Dev server middleware maps `/data-<slug>/*` → `out_<slug>/*` for any
slug, and `?city=<slug>` in the URL sets the active slug. Switching
cities is a URL change, no env-var dance.

**Alternatives**:

1. `PLATEAU_DATA_DIR=...` env var at vite startup (original v1).
   Rejected for UX — every switch is a restart.
2. Dynamic data dir swap at runtime (no restart). Rejected for
   complexity — gigabyte-scale tileset disposal + swap is brittle;
   page reload is cheaper and cleaner.

## D10 — One catalog, all consumers

`src/plateau_parquet/catalog_registry.json` is the only source of truth
for PLATEAU dataset URLs. Demos' `cities.ts` is a separate, much
smaller manifest (slug + centroid + label) generated by hand from
the catalog. They're NOT auto-synced; the cities.ts entries are
regenerated from `out_<slug>/buildings.parquet` via
`scripts/refresh_cities_ts.py` after a new city is built.

**Why not auto-sync**: the demos need extra data (centroid lat/lon,
human label) that the build-side catalog doesn't carry, and round-
tripping it from a manifest each demo loads is more plumbing than
the catalog-side benefit. Tolerable for 29 cities; revisit at 100+.

