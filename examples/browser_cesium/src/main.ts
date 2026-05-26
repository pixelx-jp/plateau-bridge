/**
 * Cesium demo for Gate B output.
 *
 * Same data contract as the three.js + deck.gl examples — load tileset.json,
 * and on every tile loaded fetch the matching per-tile Arrow style table.
 * Cesium handles ECEF natively (it's the reference 3D Tiles renderer), so
 * we don't have to fight a coordinate system.
 */

import {
  Viewer,
  Cesium3DTileset,
  Cesium3DTileStyle,
  Color,
  ScreenSpaceEventHandler,
  ScreenSpaceEventType,
  Cesium3DTileFeature,
} from "cesium";
import "cesium/Build/Cesium/Widgets/widgets.css";
import { tableFromIPC } from "apache-arrow";
import { activeCity, mountCityPicker } from "./cities";

const CITY = activeCity();
const DATA_BASE = `/data-${CITY.slug}`;
const TILESET_URL = `${DATA_BASE}/3dtiles/tileset.json`;

// Cesium serves static widget assets from this URL; vite-plugin-cesium sets
// it up under /cesium so we don't need Ion.
(globalThis as any).CESIUM_BASE_URL = "/cesium";

// Surface async errors loudly — Cesium swallows them into a single opaque
// "RuntimeError" on `pageerror`.
window.addEventListener("unhandledrejection", (ev) => {
  const r: any = ev.reason;
  console.error("[plateau-cesium] unhandledrejection:",
    String(r?.message ?? r), "name=", r?.name, "stack=", String(r?.stack ?? "").slice(0, 1500));
});
window.addEventListener("error", (ev) => {
  console.error("[plateau-cesium] window.error:", ev.message, ev.error?.stack?.slice(0, 1500));
});

async function main(): Promise<void> {
  console.log("[plateau-cesium] before Viewer ctor");
  const viewer = new Viewer("app", {
    baseLayerPicker: false,
    geocoder: false,
    timeline: false,
    animation: false,
    homeButton: false,
    sceneModePicker: false,
    fullscreenButton: false,
    navigationHelpButton: false,
    // Cesium's default infoBox dumps every EXT_structural_metadata property
    // (including JSON-stringified PLATEAU sub-attribute blobs that span
    // hundreds of characters). We render our own curated popup instead.
    infoBox: false,
    selectionIndicator: true,
    // Without these, Cesium tries to fetch Cesium Ion default imagery /
    // terrain on first render and throws RuntimeError (no Ion token).
    baseLayer: false as any,
  });
  // Remove the default Ion terrain after construction (constructor option
  // `terrain` isn't honoured in older Cesium and may itself trigger Ion).
  (viewer.scene as any).terrainProvider = undefined;
  viewer.scene.backgroundColor = Color.fromCssColorString("#0c0e12");
  const legendEl = document.getElementById("legend");
  if (legendEl) mountCityPicker(legendEl, CITY);
  console.log("[plateau-cesium] viewer ready, city =", CITY.slug);

  const resp = await fetch(`${DATA_BASE}/tile_index.json`);
  const tileIndex: Record<string, string> = await resp.json();
  console.log("[plateau-cesium] tile_index entries:", Object.keys(tileIndex).length);

  const tileset = await Cesium3DTileset.fromUrl(TILESET_URL, {});
  console.log("[plateau-cesium] tileset constructed");
  viewer.scene.primitives.add(tileset);

  // Cesium's default directional sun makes the city look pitch-black at
  // night-side longitudes. We want the height-coded colour to dominate.
  (tileset as any).shadows = 0;                         // SHADOWMODE.DISABLED
  (viewer.scene as any).light = undefined as any;       // no directional shading
  (viewer.scene as any).globe.enableLighting = false;

  // Cesium's 3D-Tiles expression language has NO `defined()` builtin —
  // it errors out with "Unexpected function call 'defined'". And a raw
  // `${measuredHeight} < 5` on a feature whose `height` property is missing
  // throws RuntimeError with "Operator '<' requires number arguments".
  // The robust pattern is `Number(${measuredHeight}) < 5` — `Number(undefined)`
  // returns NaN, and `NaN < x` is always false, so undefined features
  // fall through to the catch-all "true" branch (grey).
  tileset.style = new Cesium3DTileStyle({
    color: {
      conditions: [
        ["Number(${measuredHeight}) < 5",   "color('#0d0887')"],
        ["Number(${measuredHeight}) < 15",  "color('#5402a3')"],
        ["Number(${measuredHeight}) < 25",  "color('#9c179e')"],
        ["Number(${measuredHeight}) < 35",  "color('#cd4071')"],
        ["Number(${measuredHeight}) < 45",  "color('#ed6886')"],
        ["Number(${measuredHeight}) < 55",  "color('#fb9f3a')"],
        ["Number(${measuredHeight}) < 70",  "color('#fdc527')"],
        ["Number(${measuredHeight}) >= 70", "color('#f0f921')"],
        ["true",                    "color('#374151')"],  // NaN / unknown
      ],
    },
  });

  type StyleEntry = { rows: Map<number, Record<string, any>> };
  const styleCache = new Map<string, Promise<StyleEntry>>();
  function fetchStyle(rel: string): Promise<StyleEntry> {
    const hit = styleCache.get(rel);
    if (hit) return hit;
    const p = (async () => {
      const r = await fetch(`${DATA_BASE}/${rel}`);
      if (!r.ok) throw new Error(`${rel}: ${r.status}`);
      const buf = new Uint8Array(await r.arrayBuffer());
      const table = tableFromIPC(buf);
      const rows = new Map<number, Record<string, any>>();
      for (const row of table) {
        const obj = row.toJSON();
        rows.set(Number(obj.tile_feature_id), obj);
      }
      return { rows };
    })();
    styleCache.set(rel, p);
    return p;
  }

  let loadedCount = 0;
  const counterEl = document.getElementById("counter");
  tileset.tileLoad.addEventListener((tile: any) => {
    loadedCount += 1;
    if (counterEl) counterEl.textContent = String(loadedCount);
    const uri: string | undefined =
      tile?._contentResource?._url ?? tile?._header?.content?.uri;
    if (!uri) return;
    for (const key of Object.keys(tileIndex)) {
      if (uri.endsWith(key)) {
        void fetchStyle(tileIndex[key]).catch(() => {});
        break;
      }
    }
  });

  // ─── Custom click popup ────────────────────────────────────────────────
  // PLATEAU's EXT_structural_metadata exposes ~15 verbose properties on
  // every feature including JSON-stringified sub-attribute blobs. Showing
  // them all is unreadable. We pick the few that matter and render them
  // in a fixed-position panel; the underlying scene.pick + getProperty
  // API is the same one a real downstream app would use.
  const infoEl = document.createElement("div");
  Object.assign(infoEl.style, {
    position: "fixed", top: "12px", right: "12px",
    padding: "12px 28px 12px 16px",
    background: "rgba(20,22,28,0.92)",
    borderRadius: "8px", fontSize: "12px", lineHeight: "1.6",
    maxWidth: "340px", color: "#e7e9ee",
    fontFamily: "ui-monospace, monospace",
    display: "none", zIndex: "10",
  });
  document.body.appendChild(infoEl);

  function showInfo(html: string): void {
    infoEl.innerHTML =
      `<button id="cesium-info-close" style="position:absolute;top:4px;right:6px;background:none;border:none;color:#9ca3af;font-size:18px;cursor:pointer">×</button>` + html;
    infoEl.style.display = "block";
    const btn = document.getElementById("cesium-info-close");
    if (btn) btn.addEventListener("click", () => { infoEl.style.display = "none"; });
  }

  const handler = new ScreenSpaceEventHandler(viewer.scene.canvas);
  handler.setInputAction((click: { position: { x: number; y: number } }) => {
    const picked = viewer.scene.pick(click.position);
    if (!(picked instanceof Cesium3DTileFeature)) {
      infoEl.style.display = "none";
      return;
    }
    const get = (name: string): string | null => {
      try {
        const v = picked.getProperty(name);
        return v == null ? null : String(v);
      } catch { return null; }
    };
    const gmlId = get("gml_id") ?? get("id") ?? "?";
    const usage = get("usage");
    const measuredH = get("measuredHeight");
    const cls = get("class");
    const floorsAbove = get("storeysAboveGround");
    const lines = [
      `<strong>uid</strong> …${String(gmlId).slice(-12)}`,
      measuredH && Number(measuredH) > 0
        ? `<strong>measured height</strong> ${Number(measuredH).toFixed(1)} m`
        : "",
      floorsAbove && Number(floorsAbove) > 0 && Number(floorsAbove) < 200
        ? `<strong>floors above</strong> ${floorsAbove}`
        : "",
      usage ? `<strong>usage</strong> ${usage}` : "",
      cls ? `<strong>class</strong> ${cls}` : "",
    ].filter(Boolean).join("<br/>");
    showInfo(lines || "<em>no attributes available</em>");
  }, ScreenSpaceEventType.LEFT_CLICK);

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") infoEl.style.display = "none";
  });

  // flyTo(tileset) fits the entire bounding sphere — for a city-sized
  // tileset that puts the camera kilometres up and lighting goes bleak.
  // Pass an HeadingPitchRange offset that brings the camera in closer.
  const { HeadingPitchRange, Math: CMath } = await import("cesium");
  await viewer.flyTo(tileset, {
    duration: 1.5,
    offset: new HeadingPitchRange(
      CMath.toRadians(20),
      CMath.toRadians(-35),
      1800,
    ),
  });
  console.log("[plateau-cesium] flyTo complete");
}

main().catch((e) => {
  console.error("[plateau-cesium] main() failed:",
    String(e?.message ?? e), "name=", e?.name, "stack=", String(e?.stack ?? "").slice(0, 2000));
});
