#!/usr/bin/env node
/**
 * Patch loaders.gl 4.x's broken `getPropertyDataString` to return [] on
 * unsupported STRING[] property table columns, instead of throwing
 * `Not implemented - arrayOffsets for strings is specified` and killing
 * the whole tile.
 *
 * Why this is a once-off patch script (not a runtime vite/esbuild
 * plugin): esbuild's depscan worker runs plugin callbacks in an IPC
 * context that can't `require('fs')` reliably. Several plugin-based
 * approaches were tried (vite transform, esbuild plugin inline, esbuild
 * plugin from separate .mjs) — all hit "Dynamic require of fs is not
 * supported" inside esbuild's setup boundary. A pre-build script that
 * mutates node_modules sidesteps all of that.
 *
 * Idempotent: re-runs are no-ops once the file is patched.
 *
 * The patch is surgical — one line, one specific function. If
 * loaders.gl ever fixes upstream, this script's grep will find nothing
 * and exit zero (no-op).
 *
 * Usage:
 *   node loaders-gl-patch.mjs           # invoked by `predev` / `prebuild`
 */
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const CANDIDATES = [
  // pnpm layout
  "node_modules/.pnpm/@loaders.gl+gltf@4.4.2_@loaders.gl+core@4.4.2/node_modules/@loaders.gl/gltf/dist/lib/extensions/utils/3d-tiles-utils.js",
  // npm/yarn flat layout
  "node_modules/@loaders.gl/gltf/dist/lib/extensions/utils/3d-tiles-utils.js",
];

const BROKEN = "throw new Error('Not implemented - arrayOffsets for strings is specified');";
const MARKER = "/* plateau-parquet: STRING[] property table columns ignored */";
const PATCH = `return []; ${MARKER}`;

let patched = 0;
let already = 0;
let skipped = 0;
for (const rel of CANDIDATES) {
  const abs = resolve(HERE, rel);
  if (!existsSync(abs)) { skipped += 1; continue; }
  const src = readFileSync(abs, "utf8");
  if (src.includes(MARKER)) { already += 1; continue; }
  if (!src.includes(BROKEN)) {
    // loaders.gl version drift — upstream might have fixed it. No-op.
    skipped += 1;
    continue;
  }
  writeFileSync(abs, src.replace(BROKEN, PATCH), "utf8");
  patched += 1;
  console.log(`patched: ${rel}`);
}
if (patched + already === 0) {
  console.error("plateau-loaders-gl-patch: nothing to patch — loaders.gl layout unknown or already fixed upstream");
  process.exit(0);  // not a hard error; demo can still try without the patch
}
console.log(`plateau-loaders-gl-patch: ${patched} file(s) patched, ${already} already-patched, ${skipped} skipped`);
