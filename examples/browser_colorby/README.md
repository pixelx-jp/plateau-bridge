# Browser `colorBy` demo (three.js / r3f)

The canonical PLATEAU 3D Tiles viewer for `plateau-bridge`. Loads
nusamai-emitted glb tiles, shades each one by **per-vertex altitude**, and
on click resolves the picked `_FEATURE_ID_0` against the per-tile Arrow
side-table to show curated building attributes.

Sister demos: [`browser_cesium/`](../browser_cesium/) (CesiumJS) and
[`browser_deckgl/`](../browser_deckgl/) (deck.gl). All three read the
same `out_<city>/` bundle.

## Run

```bash
# 1. Build a city bundle from the repo root.
plateau build 13113 --out ./out_shibuya

# 2. Install + dev server. Vite middleware serves /data-<slug>/* from out_<slug>/.
cd examples/browser_colorby
pnpm install
pnpm dev          # → http://localhost:5173
```

Switch cities via URL: `http://localhost:5173/?city=osaka`. Available
slugs are listed in `src/cities.ts` (catalog manifest). The dropdown in
the top-right does the same thing.

## What's rendered

- **Geometry**: PLATEAU 3D Tiles 1.1 via [3d-tiles-renderer](https://github.com/NASA-AMMOS/3DTilesRendererJS)
  with `ReorientationPlugin` so the city sits flat at the origin.
- **Colour**: a custom `ShaderMaterial` that maps each vertex's local
  altitude to a magma ramp. **No per-feature lookup is involved in
  base colouring** — see "Why per-vertex" below.
- **Click highlight**: BVH-accelerated raycast → `_FEATURE_ID_0` →
  uniform `uHighlightFid` → fragment shader overrides matched verts
  to emerald.
- **Popup**: same `_FEATURE_ID_0` keyed into `style/<encoded>.arrow`
  via the per-tile side index; shows `building_uid`, `height`,
  `usage`, hazard coverage.

## Data contract (the only thing worth copying)

```ts
import { tableFromIPC } from "apache-arrow";

// 1. On startup, load the tile_index.
const tileIndex: Record<string, string> =
  await fetch("/data-shibuya/tile_index.json").then(r => r.json());

// 2. On tile load, the renderer hands you a content URI.
const styleRel = tileIndex[tile.content.uri];            // -> "style/<encoded>.arrow"
const buf = new Uint8Array(
  await fetch(`/data-shibuya/${styleRel}`).then(r => r.arrayBuffer())
);
const table = tableFromIPC(buf);

// 3. Per-feature attribute lookup keyed by tile_feature_id.
```

This contract is renderer-agnostic. The Cesium and deck.gl sibling
demos consume the same `out_<city>/` bundle.

## Why per-vertex altitude (not per-feature height)

PLATEAU's `bldg:Building` features are **administrative parcels**,
not visual building masses. A single tower is regularly split into
2–12 features with very different `measuredHeight` values (a 9 m
podium next to a 243 m tower in Shibuya is a real case). Any
per-feature colour exposes the admin seam as a colour cliff. Merging
by "touching footprints" is a lossy heuristic — small ε misses cases,
large ε bridges alleys.

Per-vertex altitude shading sidesteps the entire class of bug: the
shader literally cannot produce a colour cliff at an admin boundary
because it has no concept of features. The full rationale plus
alternatives considered (and why each was rejected) is in
[docs/architecture.md](../../docs/architecture.md) §D3–D4.

## Verified data plumbing

Vite middleware serves every artifact with correct headers (verified
against real Shibuya 2023 output):

```
GET /data-<slug>/tile_index.json                          → application/json
GET /data-<slug>/3dtiles/tileset.json                     → application/json
GET /data-<slug>/3dtiles/<z>/<x>/<y>_bldg_Building.glb    → model/gltf-binary
GET /data-<slug>/style/<encoded>.arrow                    → application/vnd.apache.arrow.file
```

All carry `Access-Control-Allow-Origin: *` and `Accept-Ranges: bytes`,
matching the Gate C deployment checklist in `docs/architecture.md`.

## Why not put attrs in PMTiles?

PMTiles is a *spatial-tile index*; querying it by
`(tile_uri, feature_id)` is a category error. See
[docs/architecture.md](../../docs/architecture.md) §D6 for the longer
answer.

## Attribution

The demo auto-renders `© Project PLATEAU / MLIT (CC BY 4.0)` in the
bottom right per the project's attribution rules. plateau-bridge itself
is © PixelX Inc. / Yodo Labs, MIT-licensed.
