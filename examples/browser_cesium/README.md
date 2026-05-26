# Browser demo (CesiumJS)

PLATEAU 3D Tiles in CesiumJS, with `Cesium3DTileStyle` height-based shading
and a custom click popup wired to `EXT_structural_metadata`. Sister to
[`browser_colorby/`](../browser_colorby/) (three.js / r3f) and
[`browser_deckgl/`](../browser_deckgl/). All three read the same
`out_<city>/` bundle.

## Run

```bash
plateau build 13113 --out ../../out_shibuya   # if not already built
pnpm install
pnpm dev          # → http://localhost:5174
```

Switch cities via `?city=<slug>` — e.g. `http://localhost:5174/?city=osaka`.

## What this demonstrates

- **3D Tiles 1.1 native rendering.** Cesium is the reference 3D Tiles
  implementation. PLATEAU tiles load directly — no `ReorientationPlugin`,
  no ECEF gymnastics.
- **Per-feature styling via `Cesium3DTileStyle`.** Height-based magma
  palette evaluated at draw time using `${measuredHeight}` against the
  glb's embedded per-feature metadata.
- **Custom click popup.** `scene.pick` → `Cesium3DTileFeature.getProperty`,
  showing 5 curated lines (`building_uid`, `measuredHeight`, `usage`,
  hazard coverage). Replaces Cesium's default info-box, which dumps every
  raw EXT_structural_metadata field including JSON-stringified
  sub-attribute blobs.

## Compared to the other two demos

|                     | three.js (`browser_colorby`)       | Cesium (this)                              | deck.gl (`browser_deckgl`)              |
| ---                 | ---                                | ---                                        | ---                                     |
| Per-feature shading | Custom shader + altitude          | `Cesium3DTileStyle` height expressions     | Monochrome (loaders.gl limitation)      |
| Click → highlight   | Yes (BVH-accelerated raycast)      | Selection outline (Cesium default)         | None                                    |
| Click → popup       | Curated, from Arrow side-table     | Curated, from EXT_structural_metadata      | Curated, from Arrow side-table          |
| Setup complexity    | Medium                             | Medium (Cesium widget assets)              | Low                                     |
| Bundle size         | ~400 KB                            | ~3 MB                                      | ~600 KB                                 |

The point of three demos isn't redundancy — it's to **prove the data
contract** (`tileset.json` + per-tile Arrow style tables + `tile_index.json`)
is renderer-agnostic.

## Gotchas (re-verify on Cesium upgrades)

- **Cesium has no `defined()` builtin.** The 3D-Tiles styling spec lists
  it but Cesium throws `RuntimeError: Unexpected function call "defined"`.
  Use `Number(${prop}) < n` (undefined → NaN → false). The full pattern
  is inline in `src/main.ts`.
- **Cesium reaches Cesium Ion by default.** `baseLayer` and `terrainProvider`
  fetch on construction; without an Ion token they throw a hard
  `RuntimeError` on first render. Pass `baseLayer: false` to the
  `Viewer` constructor (we do).
- **PLATEAU's EXT_structural_metadata property is `measuredHeight`, not
  `height`.** Style expressions reference `${measuredHeight}`. Re-verify
  if nusamai's schema changes.

## Attribution

`© Project PLATEAU / MLIT (CC BY 4.0)` auto-rendered per project rules.
plateau-bridge itself is © PixelX Inc. / Yodo Labs, MIT-licensed.
