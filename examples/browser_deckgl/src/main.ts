/**
 * deck.gl Tile3DLayer demo for plateau-parquet Gate B output.
 *
 * PLATEAU 3D Tiles are in ECEF (Cesium 3D Tiles 1.1). deck.gl's Tile3DLayer
 * handles the geographic transform automatically — what we need is just a
 * geographic MapView centered on the city. The legend overlay shows that
 * the layer is alive (counter increments per loaded tile).
 */

import { Deck, MapView } from "@deck.gl/core";
import { Tile3DLayer } from "@deck.gl/geo-layers";
import { Tiles3DLoader } from "@loaders.gl/3d-tiles";
import { tableFromIPC } from "apache-arrow";
import { activeCity, mountCityPicker } from "./cities";

const CITY = activeCity();
const DATA_BASE = `/data-${CITY.slug}`;
const TILESET_URL = `${DATA_BASE}/3dtiles/tileset.json`;
const ORIGIN: [number, number] = [CITY.lon, CITY.lat];

const legendEl = document.getElementById("legend");
if (legendEl) mountCityPicker(legendEl, CITY);

const MAGMA: [number, number, number][] = [
  [13, 8, 135], [84, 2, 163], [156, 23, 158], [205, 52, 121],
  [237, 104, 87], [251, 159, 58], [253, 215, 56], [240, 249, 33],
];

function magmaColor(v: number, vmin = 0, vmax = 60): [number, number, number] {
  const t = Math.max(0, Math.min(1, (v - vmin) / (vmax - vmin)));
  const i = t * (MAGMA.length - 1);
  const i0 = Math.floor(i), i1 = Math.min(i0 + 1, MAGMA.length - 1);
  const f = i - i0;
  return [
    Math.round(MAGMA[i0][0] + (MAGMA[i1][0] - MAGMA[i0][0]) * f),
    Math.round(MAGMA[i0][1] + (MAGMA[i1][1] - MAGMA[i0][1]) * f),
    Math.round(MAGMA[i0][2] + (MAGMA[i1][2] - MAGMA[i0][2]) * f),
  ];
}

const tileIndex: Record<string, string> = await fetch(`${DATA_BASE}/tile_index.json`).then(r => r.json());

const colorCache = new Map<string, Map<number, [number, number, number]>>();
// Companion cache holding the raw Arrow rows for click-popup use.
const arrowCache = new Map<string, Map<number, Record<string, any>>>();
let loadedCount = 0;
const counterEl = document.getElementById("counter");

async function colorsForTile(uri: string) {
  if (colorCache.has(uri)) return colorCache.get(uri)!;
  const rel = tileIndex[uri];
  if (!rel) return null;
  try {
    const buf = new Uint8Array(await fetch(`${DATA_BASE}/${rel}`).then(r => r.arrayBuffer()));
    const t = tableFromIPC(buf);
    const map = new Map<number, [number, number, number]>();
    const rows = new Map<number, Record<string, any>>();
    for (const row of t) {
      const obj = row.toJSON();
      const fid = Number(obj.tile_feature_id);
      const h = obj.height == null ? null : Number(obj.height);
      map.set(fid, h == null ? [120, 128, 140] : magmaColor(h, 0, 60));
      rows.set(fid, obj);
    }
    colorCache.set(uri, map);
    arrowCache.set(uri, rows);
    return map;
  } catch {
    return null;
  }
}


// Per-feature picking + popup not wired in v1: Tile3DLayer's picker only
// surfaces the tile, not the per-vertex feature ID. Wiring requires a
// custom Tile3DLayer subclass that decorates the picking buffer. Deferred
// to the backlog; see HANDOFF.md.

new Deck({
  parent: document.getElementById("app")!,
  views: new MapView({ id: "main", controller: true }),
  initialViewState: {
    longitude: ORIGIN[0],
    latitude: ORIGIN[1],
    zoom: 15,
    pitch: 60,
    bearing: 20,
  },
  controller: true,
  // Click anywhere on the canvas → ask deck.gl for the picked feature.
  onClick: (info: any) => {
    console.log("[deck] click", { picked: info?.picked, fid: info?.featureId, layer: info?.layer?.id, keys: Object.keys(info ?? {}) });
    if (!info?.picked) {
      infoEl.style.display = "none";
      return;
    }
    if (info.featureId == null) {
      showInfo(`<em>picked tile but no feature id (keys: ${Object.keys(info).join(", ")})</em>`);
      return;
    }
    // `info.sourceLayer` is the underlying ScenegraphLayer; its tile URI
    // is hidden in the layer.id. We extract it and look up the Arrow
    // side-table row for the picked feature_id.
    const layerId: string = info.layer?.id ?? "";
    // tile-3d-layer creates sub-layers with ids like "plateau-3d-tiles-<tileId>"
    // — we need the underlying tile URI, which deck.gl doesn't expose
    // directly. Fall back to "find any cached colour map whose key set
    // contains this feature_id" — coarse but works for v1.
    const fid = Number(info.featureId);
    const lines: string[] = [];
    for (const [uri, _] of colorCache) {
      const t = arrowCache.get(uri);
      if (!t) continue;
      const row = t.get(fid);
      if (!row) continue;
      lines.push(`<strong>uid</strong> …${String(row.building_uid).slice(-12)}`);
      if (row.height != null) lines.push(`<strong>height</strong> ${Number(row.height).toFixed(1)} m`);
      if (row.usage) lines.push(`<strong>usage</strong> ${row.usage}`);
      if (row.river_flood_covered) {
        lines.push(`<strong>river_flood</strong> covered${row.river_flood_depth_max ? `, depth ≤ ${row.river_flood_depth_max} m` : " — surveyed safe"}`);
      }
      break;
    }
    void layerId;
    showInfo(lines.length ? lines.join("<br/>") : `<em>feature ${fid} — no side-table match</em>`);
  },
  layers: [
    new Tile3DLayer({
      id: "plateau-3d-tiles",
      data: TILESET_URL,
      loader: Tiles3DLoader,
      // vite.config.ts contains a `plateau-loaders-gl-patch` plugin that
      // monkey-patches loaders.gl's `getPropertyDataString` to return []
      // on the unsupported STRING[] case instead of throwing. That keeps
      // numeric EXT_structural_metadata columns (measuredHeight etc.)
      // available to deck.gl picking, AND keeps EXT_mesh_features around
      // so per-vertex feature IDs work — no `excludeExtensions` needed.
      pickable: true,
      onTileLoad: async (tileHeader: any) => {
        const uri = tileHeader?.content?.uri ?? tileHeader?.contentUri;
        if (uri) {
          await colorsForTile(uri);
          loadedCount += 1;
          if (counterEl) counterEl.textContent = String(loadedCount);
        }
      },
      _lighting: "pbr",
      getColor: [200, 210, 230, 255],
    }),
  ],
});
void deck;
window.addEventListener("keydown", (e) => { if (e.key === "Escape") infoEl.style.display = "none"; });
