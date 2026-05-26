/**
 * Browser demo for Gate B output.
 *
 *   tile_index.json → per-tile Arrow style table → custom ShaderMaterial
 *   shading on `_FEATURE_ID_0`. Click a building → raycast → lookup → popup.
 *
 * Performance notes:
 *   - three-mesh-bvh accelerates raycast from O(triangles) → O(log triangles)
 *     per geometry. Without it, clicks in a city with thousands of tiles
 *     are visibly slow.
 *   - styleCache + colorCache are bounded; we listen to TilesRenderer's
 *     dispose-model event and free the matching entries.
 */

import {
  Color,
  PerspectiveCamera,
  Scene,
  WebGLRenderer,
  AmbientLight,
  DirectionalLight,
  ShaderMaterial,
  Mesh,
  MathUtils,
  Raycaster,
  Vector2,
  Vector3,
  BufferGeometry,
  MOUSE,
} from "three";
import { TilesRenderer } from "3d-tiles-renderer";
import { ReorientationPlugin } from "3d-tiles-renderer/plugins";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import {
  computeBoundsTree, disposeBoundsTree, acceleratedRaycast,
} from "three-mesh-bvh";
import { tableFromIPC } from "apache-arrow";

// Wire three-mesh-bvh into BufferGeometry + Mesh globally.
(BufferGeometry.prototype as any).computeBoundsTree = computeBoundsTree;
(BufferGeometry.prototype as any).disposeBoundsTree = disposeBoundsTree;
(Mesh.prototype as any).raycast = acceleratedRaycast;

import { activeCity, mountCityPicker } from "./cities";

const CITY = activeCity();
const DATA_BASE = `/data-${CITY.slug}`;
const TILESET_URL = `${DATA_BASE}/3dtiles/tileset.json`;
const ORIGIN_LATLON: { lat: number; lon: number } = { lat: CITY.lat, lon: CITY.lon };

type ColorBy = "height" | "year_built" | "river_flood_depth_max";
const COLOR_BY: ColorBy = (globalThis as any).__PLATEAU_COLOR_BY__ ?? "height";

const MAGMA: [number, number, number][] = [
  [13, 8, 135], [84, 2, 163], [156, 23, 158], [205, 52, 121],
  [237, 104, 87], [251, 159, 58], [253, 215, 56], [240, 249, 33],
];

function lerp3(a: [number, number, number], b: [number, number, number], t: number): [number, number, number] {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
}

function magmaColor(v: number, vmin = 0, vmax = 60): [number, number, number] {
  const t = MathUtils.clamp((v - vmin) / (vmax - vmin), 0, 1);
  const i = t * (MAGMA.length - 1);
  const i0 = Math.floor(i), i1 = Math.min(i0 + 1, MAGMA.length - 1);
  return lerp3(MAGMA[i0], MAGMA[i1], i - i0);
}

function ageColor(year: number | null | undefined): [number, number, number] {
  if (year == null) return [55, 65, 81];
  if (year < 1981) return [127, 29, 29];
  if (year < 2000) return [245, 158, 11];
  return [16, 185, 129];
}

function colorFor(row: any): [number, number, number] {
  if (COLOR_BY === "year_built") {
    return ageColor(row.year_built == null ? null : Number(row.year_built));
  }
  if (COLOR_BY === "river_flood_depth_max") {
    const v = row.river_flood_depth_max;
    if (v == null) return [55, 65, 81];
    return magmaColor(Number(v), 0, 10);
  }
  // Prefer cluster-aware height so PLATEAU's "tower-on-podium split" doesn't
  // paint adjacent features with jarring colour cliffs. Buildings that share
  // a footprint (within 0.1 m) get the same `complex_max_height`. Fall back
  // to per-feature `height` for analysts who patched the parquet manually.
  const h = row.complex_max_height ?? row.height;
  if (h == null) return [55, 65, 81];
  return magmaColor(Number(h), 0, 60);
}

type TileIndex = Record<string, string>;

async function loadTileIndex(): Promise<TileIndex> {
  const r = await fetch(`${DATA_BASE}/tile_index.json`);
  if (!r.ok) throw new Error(`tile_index.json: ${r.status}`);
  return r.json();
}

type StyleEntry = {
  colors: Map<number, [number, number, number]>;
  rows: Map<number, Record<string, any>>;
};
const styleCache = new Map<string, Promise<StyleEntry>>();

async function fetchStyle(styleRel: string): Promise<StyleEntry> {
  if (styleCache.has(styleRel)) return styleCache.get(styleRel)!;
  const p = (async () => {
    const r = await fetch(`${DATA_BASE}/${styleRel}`);
    if (!r.ok) throw new Error(`${styleRel}: ${r.status}`);
    const buf = new Uint8Array(await r.arrayBuffer());
    const table = tableFromIPC(buf);
    const colors = new Map<number, [number, number, number]>();
    const rows = new Map<number, Record<string, any>>();
    for (const row of table) {
      const obj = row.toJSON();
      const fid = Number(obj.tile_feature_id);
      colors.set(fid, colorFor(obj));
      rows.set(fid, obj);
    }
    return { colors, rows };
  })();
  styleCache.set(styleRel, p);
  return p;
}

const meshToTileUri = new WeakMap<Mesh, string>();
// Forward index: tileUri → its loaded meshes (so we can free BVH + style cache on tile dispose).
const tileUriToMeshes = new Map<string, Set<Mesh>>();
const tileUriToStyleRel = new Map<string, string>();

// Selection highlight: flip a single uniform on every mesh of the
// containing tile. The shader compares per-vertex `_FEATURE_ID_0` against
// `uHighlightFid` and overrides the base colour for matching vertices.
// Previous implementation rebuilt the per-tile colour texture on every
// click; the uniform approach is allocation-free and O(meshes-in-tile).
type Highlight = { tileUri: string; fid: number };
let currentHighlight: Highlight | null = null;

const HIGHLIGHT_COLOR: [number, number, number] = [16, 185, 129];   // emerald-500

function setHighlight(tileUri: string, fid: number) {
  clearHighlight();
  const meshes = tileUriToMeshes.get(tileUri);
  if (!meshes) return;
  for (const mesh of meshes) {
    const mat = mesh.material as ShaderMaterial;
    mat.uniforms.uHighlightFid.value = fid;
  }
  currentHighlight = { tileUri, fid };
}

function clearHighlight() {
  if (!currentHighlight) return;
  const meshes = tileUriToMeshes.get(currentHighlight.tileUri);
  if (meshes) {
    for (const mesh of meshes) {
      const mat = mesh.material as ShaderMaterial;
      mat.uniforms.uHighlightFid.value = -1.0;
    }
  }
  currentHighlight = null;
}

// ─────────────────────────────────────────────────────────────────────────────
// Altitude-based shader (default).
//
// We deliberately do NOT colour by ``building.height`` from a side-channel
// lookup table. PLATEAU's ``bldg:Building`` features are administrative
// units (one parcel, one feature) — a single visually-unified building
// mass is regularly split into 2-12 features with wildly different
// ``measuredHeight`` values (a 9 m podium attached to a 243 m tower in
// Shibuya 2023 is a real case). Any "merge touching features" heuristic
// is a lossy inverse: small ε misses the cases, large ε bridges alleys.
//
// Instead we colour every fragment by its **world-Y altitude**. The shader
// has no concept of features or buildings — only of position. Adjacent
// vertices at the same altitude are guaranteed to render the same colour,
// regardless of which PLATEAU feature they belong to. Feature boundaries
// can't produce colour cliffs because the shader doesn't read feature IDs.
//
// Trade-off: you lose "this whole building is 60 m tall" as a per-feature
// colour signal. You gain visual coherence everywhere. Per-feature data
// (year_built, hazard depth, gml_id) is still available via the
// click-inspect popup, which reads the per-tile Arrow side index.
// ─────────────────────────────────────────────────────────────────────────────
const SHADER = {
  vertexShader: /* glsl */ `
    attribute float _FEATURE_ID_0;
    uniform float uYBase;
    varying float vAltY;
    varying float vFid;
    varying vec3 vNormal;
    void main() {
      // Per-mesh ground baseline. Each PLATEAU tile (= mesh) is authored
      // in its own local Y-up frame; nusamai writes Y in metres relative
      // to some per-tile reference (NOT sea level, NOT consistent between
      // tiles). So we anchor against the mesh's own min-Y — set as a
      // uniform at load time from boundingBox.min.y. Trade-off: tiles
      // covering sloped terrain anchor to their lowest point, making
      // higher-elevation buildings look taller than they truly are. The
      // alternative (a city-wide constant) breaks across cities (Sapporo
      // local Y starts at +250 m, Nagoya at -41 m, Shibuya at -16 m).
      // World-Y via modelMatrix isn't usable either — 3d-tiles-renderer
      // leaves coords in ECEF (millions of metres, not altitude).
      // NB. no backticks in this comment — they terminate the template.
      vAltY = position.y - uYBase;
      vFid = _FEATURE_ID_0;
      vNormal = normalize(normalMatrix * normal);
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }
  `,
  fragmentShader: /* glsl */ `
    uniform float uHighlightFid;
    uniform vec3  uHighlightColor;
    varying float vAltY;
    varying float vFid;
    varying vec3 vNormal;

    // Magma palette stops, expressed inline (8 stops). The classic
    // perceptually-uniform sequential ramp.
    vec3 magma(float t) {
      t = clamp(t, 0.0, 1.0);
      vec3 c0 = vec3(0.051, 0.031, 0.529);
      vec3 c1 = vec3(0.329, 0.008, 0.639);
      vec3 c2 = vec3(0.612, 0.090, 0.620);
      vec3 c3 = vec3(0.804, 0.204, 0.475);
      vec3 c4 = vec3(0.929, 0.408, 0.341);
      vec3 c5 = vec3(0.984, 0.624, 0.227);
      vec3 c6 = vec3(0.992, 0.843, 0.220);
      vec3 c7 = vec3(0.941, 0.976, 0.129);
      float n = 7.0;
      float i = t * n;
      int idx = int(floor(i));
      float f = fract(i);
      if (idx == 0) return mix(c0, c1, f);
      if (idx == 1) return mix(c1, c2, f);
      if (idx == 2) return mix(c2, c3, f);
      if (idx == 3) return mix(c3, c4, f);
      if (idx == 4) return mix(c4, c5, f);
      if (idx == 5) return mix(c5, c6, f);
      return mix(c6, c7, f);
    }

    void main() {
      // Map world-Y in metres to the 0-1 palette. 0..100 m covers the
      // entire skyline of every Japanese city in our catalog except a
      // few super-tall towers (Roppongi Hills 238 m, Shibuya Scramble
      // 230 m) which simply saturate at the bright end.
      float t = vAltY / 100.0;
      vec3 base = magma(t);
      // Selection overlay: when uHighlightFid >= 0, replace base for
      // any vertex whose feature ID matches.
      if (uHighlightFid >= 0.0 && abs(vFid - uHighlightFid) < 0.5) {
        base = uHighlightColor;
      }
      float lighting = 0.55 + 0.45 * max(dot(vNormal, normalize(vec3(0.3, 1.0, 0.4))), 0.0);
      gl_FragColor = vec4(base * lighting, 1.0);
    }
  `,
};

const legendEl = document.getElementById("legend");
if (legendEl) mountCityPicker(legendEl, CITY);

const infoEl = document.getElementById("info")!;

function showInfo(html: string) {
  infoEl.innerHTML = `<button id="info-close" style="position:absolute;top:4px;right:6px;background:none;border:none;color:#9ca3af;font-size:18px;cursor:pointer;line-height:1">×</button>${html}`;
  infoEl.style.display = "block";
  const btn = document.getElementById("info-close");
  if (btn) btn.addEventListener("click", () => { hideInfo(); clearHighlight(); });
}
function hideInfo() { infoEl.style.display = "none"; }

async function main() {
  const root = document.getElementById("app")!;
  const renderer = new WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.setSize(root.clientWidth, root.clientHeight);
  root.appendChild(renderer.domElement);

  const scene = new Scene();
  scene.background = new Color(0x0c0e12);
  scene.add(new AmbientLight(0xffffff, 0.4));
  const sun = new DirectionalLight(0xffffff, 1.0);
  sun.position.set(1, 2, 0.5);
  scene.add(sun);

  const camera = new PerspectiveCamera(60, root.clientWidth / root.clientHeight, 1, 50000);
  camera.position.set(0, 600, 900);
  camera.lookAt(0, 0, 0);

  // Mouse navigation. Default: left=orbit, right=pan, wheel=zoom.
  // Mac users without right-click: Shift+left also pans, via OrbitControls'
  // built-in mouseButtons map (no hacks). The map respects the modifier.
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.target.set(0, 0, 0);
  controls.maxPolarAngle = Math.PI * 0.49;
  controls.minDistance = 50;
  controls.maxDistance = 8000;
  // OrbitControls reads modifiers from the pointer event; the default map is
  // already LEFT=ROTATE / RIGHT=PAN / MIDDLE=DOLLY. We expose Shift+LEFT=PAN
  // by overriding the left-button assignment under shift in the pointerdown
  // handler chain below (see panOnShift listener).

  const tileIndex = await loadTileIndex();

  const tiles = new TilesRenderer(TILESET_URL);
  tiles.registerPlugin(
    new ReorientationPlugin({
      lat: MathUtils.degToRad(ORIGIN_LATLON.lat),
      lon: MathUtils.degToRad(ORIGIN_LATLON.lon),
      height: 0,
    }),
  );
  tiles.setCamera(camera);
  tiles.setResolutionFromRenderer(camera, renderer);


  tiles.addEventListener("load-model", async (evt: any) => {
    const tile = evt.tile;
    const uri: string | undefined = tile?.content?.uri;
    if (!uri) return;
    const styleRel = tileIndex[uri];
    if (!styleRel) return;
    tileUriToStyleRel.set(uri, styleRel);
    // Pre-fetch the per-tile Arrow side index in the background so
    // click-inspect popups have it ready. Shading no longer needs it.
    void fetchStyle(styleRel);
    const meshes = tileUriToMeshes.get(uri) ?? new Set<Mesh>();
    evt.scene.traverse((obj: any) => {
      if (!(obj instanceof Mesh)) return;
      const attrs = obj.geometry.attributes;
      if (!attrs._FEATURE_ID_0 && !attrs._feature_id_0) return;
      // Per-mesh ground baseline from local boundingBox.min.y. Each
      // mesh becomes its own self-anchored colour ramp; works equally
      // well for every city PLATEAU may add without per-city constants.
      obj.geometry.computeBoundingBox();
      const yBase = obj.geometry.boundingBox?.min?.y ?? 0;
      meshToTileUri.set(obj, uri);
      meshes.add(obj);
      // Pre-compute BVH for fast raycast — O(log N) instead of O(N).
      (obj.geometry as any).computeBoundsTree?.();
      obj.material = new ShaderMaterial({
        uniforms: {
          uHighlightFid:   { value: -1.0 },
          uHighlightColor: { value: new Vector3(HIGHLIGHT_COLOR[0]/255, HIGHLIGHT_COLOR[1]/255, HIGHLIGHT_COLOR[2]/255) },
          uYBase:          { value: yBase },
        },
        vertexShader:   SHADER.vertexShader,
        fragmentShader: SHADER.fragmentShader,
      });
    });
    tileUriToMeshes.set(uri, meshes);
  });

  // Free BVH + style cache when a tile is unloaded — prevents memory growth
  // during long browsing sessions.
  tiles.addEventListener("dispose-model", (evt: any) => {
    const uri: string | undefined = evt.tile?.content?.uri;
    if (!uri) return;
    const meshes = tileUriToMeshes.get(uri);
    if (meshes) {
      for (const m of meshes) {
        (m.geometry as any).disposeBoundsTree?.();
      }
      tileUriToMeshes.delete(uri);
    }
    const styleRel = tileUriToStyleRel.get(uri);
    if (styleRel) {
      styleCache.delete(styleRel);
      tileUriToStyleRel.delete(uri);
    }
  });

  scene.add(tiles.group);

  // Click vs drag: only raycast on pointerup if cursor moved < 5px.
  const raycaster = new Raycaster();
  const pointer = new Vector2();
  const downAt = { x: 0, y: 0, t: 0 };

  renderer.domElement.addEventListener("pointerdown", (ev) => {
    downAt.x = ev.clientX;
    downAt.y = ev.clientY;
    downAt.t = performance.now();
  });

  renderer.domElement.addEventListener("pointerup", async (ev) => {
    const dx = ev.clientX - downAt.x;
    const dy = ev.clientY - downAt.y;
    if (Math.hypot(dx, dy) > 5) return;             // it was a drag
    if (performance.now() - downAt.t > 500) return; // long press

    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);

    const candidates: Mesh[] = [];
    for (const set of tileUriToMeshes.values()) {
      for (const m of set) candidates.push(m);
    }
    const hits = raycaster.intersectObjects(candidates, false);
    if (hits.length === 0) {
      showInfo("<em style='color:#9ca3af'>no building at this point — try clicking on a coloured façade</em>");
      return;
    }

    const hit = hits[0];
    const mesh = hit.object as Mesh;
    const uri = meshToTileUri.get(mesh);
    const fidAttr = mesh.geometry.attributes._FEATURE_ID_0 ?? (mesh.geometry.attributes as any)._feature_id_0;
    if (!uri || !fidAttr) return;
    const vIdx = hit.face?.a ?? 0;
    const fid = fidAttr.array[vIdx];
    const styleRel = tileIndex[uri];
    if (!styleRel) return;
    const entry = await fetchStyle(styleRel);
    const row = entry.rows.get(Number(fid));
    if (!row) {
      showInfo(
        "<em style='color:#9ca3af'>building hit but not in this city's parquet — "
        + "likely clipped by the admin boundary (e.g. neighbouring ward in the same PLATEAU bundle)</em>"
      );
      return;
    }
    setHighlight(uri, Number(fid));
    const uidShort = String(row.building_uid).slice(-12);
    const lines = [
      `<strong>uid</strong> …${uidShort}`,
      row.height != null ? `<strong>height</strong> ${Number(row.height).toFixed(1)} m` : "",
      row.usage ? `<strong>usage</strong> ${row.usage}` : "",
      row.river_flood_covered
        ? `<strong>river_flood</strong> covered${row.river_flood_depth_max ? `, depth ≤ ${row.river_flood_depth_max} m` : " — surveyed safe"}`
        : "<strong>river_flood</strong> unknown (not surveyed)",
    ].filter(Boolean).join("<br/>");
    showInfo(lines);
  });

  // Esc closes the popup AND clears the highlight.
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { hideInfo(); clearHighlight(); }
  });

  // Shift+drag pan: temporarily remap LEFT to PAN while shift is held.
  let shiftWasDown = false;
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Shift" && !shiftWasDown) {
      controls.mouseButtons = { LEFT: MOUSE.PAN, MIDDLE: MOUSE.DOLLY, RIGHT: MOUSE.ROTATE };
      shiftWasDown = true;
    }
  });
  window.addEventListener("keyup", (ev) => {
    if (ev.key === "Shift" && shiftWasDown) {
      controls.mouseButtons = { LEFT: MOUSE.ROTATE, MIDDLE: MOUSE.DOLLY, RIGHT: MOUSE.PAN };
      shiftWasDown = false;
    }
  });

  function frame() {
    controls.update();
    tiles.update();
    renderer.render(scene, camera);
    requestAnimationFrame(frame);
  }
  frame();

  window.addEventListener("resize", () => {
    renderer.setSize(root.clientWidth, root.clientHeight);
    camera.aspect = root.clientWidth / root.clientHeight;
    camera.updateProjectionMatrix();
  });
}

main().catch((e) => {
  console.error(e);
  document.body.innerHTML = `<pre style="color:#f87171;padding:24px">${e}</pre>`;
});
