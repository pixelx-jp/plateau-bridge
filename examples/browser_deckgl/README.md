# Browser demo (deck.gl)

PLATEAU 3D Tiles via deck.gl's `Tile3DLayer`. **Monochrome geometry +
full per-feature property access** through a patched loaders.gl (see
"the fix" below) — click anywhere to read `measuredHeight`, `usage`
etc. straight off the picked feature. Sister to
[`browser_colorby/`](../browser_colorby/) (three.js / r3f, full
per-vertex altitude shading) and [`browser_cesium/`](../browser_cesium/).

The point of this demo is to **prove the data contract** (`tileset.json` +
`tile_index.json` + per-tile `style/*.arrow` partitions) works through a
third independent stack — not to be the prettiest of the three. For
production rendering, use the three.js demo.

## Run

```bash
plateau build 13113 --out ../../out_shibuya   # if not already built
pnpm install
pnpm dev          # → http://localhost:5175
```

Switch cities via `?city=<slug>` — e.g. `http://localhost:5175/?city=osaka`.

## Data contract (unchanged)

```ts
import { tableFromIPC } from "apache-arrow";

const tileIndex = await fetch("/data-shibuya/tile_index.json").then(r => r.json());
async function loadStyle(uri: string) {
  const rel = tileIndex[uri];
  const buf = new Uint8Array(await fetch(`/data-shibuya/${rel}`).then(r => r.arrayBuffer()));
  return tableFromIPC(buf);  // tile_feature_id → height, usage, hazard, ...
}

new Tile3DLayer({
  data: "/data-shibuya/3dtiles/tileset.json",
  pickable: true,
  onTileLoad: tile => loadStyle(tile.content.uri),
  // ...
});
```

## Why monochrome — and how we partially worked around it

deck.gl's `Tile3DLayer` delegates 3D Tiles parsing to **loaders.gl** (v4.x).
PLATEAU's nusamai converter emits `EXT_structural_metadata` property
tables with variable-length string arrays (e.g. `gml_id`), and
loaders.gl's `getPropertyDataString` throws on that one case:

```
Not implemented - arrayOffsets for strings is specified
```

The throw kills the **whole extension**, not just the unsupported
column — every tile fails to load.

### The fix: `loaders-gl-patch.mjs`

We ship a small Node script (`loaders-gl-patch.mjs`) that rewrites that
one `throw` to `return []` in the installed loaders.gl source. The
script is wired into `predev`, `prebuild`, and `postinstall` so it's
applied automatically; it's idempotent and a no-op if loaders.gl ever
fixes the issue upstream.

Net effect:
- All tiles load (geometry renders correctly).
- Numeric EXT_structural_metadata columns (`measuredHeight`,
  `storeysAboveGround`) come through normally — picking returns the
  full property bag.
- STRING[] columns (just `gml_id` in practice) silently become empty
  arrays. Since we already keep building IDs in the per-tile Arrow
  side-table, this is a non-loss.

### Why approach over a vite plugin

We tried four runtime plugin variants (vite `transform`, vite plugin
with `enforce: 'pre'`, esbuild plugin inline in `optimizeDeps`,
esbuild plugin in a separate `.mjs` module). Vite's depscan runs
plugins in an esbuild worker context that can't `require('fs')`
reliably (`Dynamic require of fs is not supported`). A pre-install
mutation of `node_modules` sidesteps the entire IPC boundary.

The trade-off is that re-installing dependencies un-patches it — but
`postinstall` re-runs the script, so the only way to lose the patch is
to delete `node_modules` *without* running `pnpm install` afterward.

### What's still missing

Per-vertex shading: deck.gl's `Tile3DLayer` doesn't expose a
ScenegraphLayer extension surface that's easy to hook a per-feature
colour into. The three.js demo's custom `ShaderMaterial` is the
production reference for that. A clean deck.gl-side equivalent
requires writing a `LayerExtension` that injects GLSL — left as a
follow-up.

## Compared to the other two demos

|                     | three.js (`browser_colorby`)       | Cesium (`browser_cesium`)                  | deck.gl (this)                          |
| ---                 | ---                                | ---                                        | ---                                     |
| Per-feature shading | Custom shader + altitude          | `Cesium3DTileStyle` height expressions     | **Monochrome** (loaders.gl limitation)  |
| Click → popup       | Curated, from Arrow side-table     | Curated, from EXT_structural_metadata      | Curated, from Arrow side-table          |
| Setup complexity    | Medium                             | Medium (Cesium widget assets)              | Low                                     |
| Bundle size         | ~400 KB                            | ~3 MB                                      | ~600 KB                                 |

## Attribution

`© Project PLATEAU / MLIT (CC BY 4.0)` auto-rendered per project rules.
plateau-bridge itself is © PixelX Inc. / Yodo Labs, MIT-licensed.
