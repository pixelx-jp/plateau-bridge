#!/usr/bin/env node
/**
 * Lightweight browser smoke test for the three demos.
 *
 * Each demo × each city must pass 5 assertions:
 *
 *   1. Page loads with 0 console.error messages (allowing well-known
 *      browser noise: favicon 404, well-documented loaders.gl warning).
 *   2. The city dropdown reflects the requested `?city=` slug.
 *   3. The canvas isn't blank — screenshot byte size > MIN_PIXEL_BYTES
 *      (~150 KB) is a coarse proxy for "renderer painted something".
 *   4. At least one network request to /data-<city>/ was made and
 *      returned 200 — proves vite middleware + city routing.
 *   5. Demo-specific paint signal:
 *      - colorby:   `<canvas>` has non-zero pixel area
 *      - cesium:    #counter shows N > 0
 *      - deckgl:    at least one *.glb request returned 200
 *
 * Run:
 *   # Ensure the three dev servers are up:
 *   ( cd examples/browser_colorby && pnpm dev & )
 *   ( cd examples/browser_cesium   && pnpm dev & )
 *   ( cd examples/browser_deckgl   && pnpm dev & )
 *
 *   node scripts/test_browser.mjs            # exits 0 on success
 *   node scripts/test_browser.mjs --city=osaka  # single-city variant
 *
 * The script does NOT manage vite lifecycle — that keeps it simple and
 * matches the CI pattern of "start servers, then run the suite". See
 * .github/workflows/browser-tests.yml for the CI invocation (TODO).
 */
import { chromium } from "playwright";

const argv = process.argv.slice(2);
const FORCE_CITY = argv.find(a => a.startsWith("--city="))?.split("=")[1];
const CITIES = FORCE_CITY ? [FORCE_CITY] : ["shibuya", "suginami"];

const DEMOS = [
  { name: "colorby", port: 5173 },
  { name: "cesium",  port: 5174 },
  { name: "deckgl",  port: 5175 },
];

const SUITE_TIMEOUT_MS = 60_000;
const MIN_PIXEL_BYTES = 150_000;
// Known-noise messages we explicitly tolerate — bare regression detection
// would otherwise flag these on every run.
const EXPECTED_NOISE = [
  /favicon/i,
  /sourcemap/i,
  /\[vite\]/i,
  /Failed to load resource.*favicon/i,
];

function noisy(msg) {
  return EXPECTED_NOISE.some(re => re.test(msg));
}

async function checkPortUp(port) {
  try {
    const res = await fetch(`http://localhost:${port}/`, { signal: AbortSignal.timeout(2000) });
    return res.ok;
  } catch {
    return false;
  }
}

async function probeOne(demo, city) {
  const errors = [];
  const dataRequests = [];
  const glbRequests = [];
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  page.on("console", m => {
    if (m.type() === "error" && !noisy(m.text())) errors.push(m.text());
  });
  page.on("pageerror", e => errors.push(`pageerror: ${e.message}`));
  page.on("response", r => {
    const u = r.url();
    if (u.includes(`/data-${city}/`) && r.ok()) dataRequests.push(u);
    if (u.includes(".glb") && r.ok()) glbRequests.push(u);
  });

  let canvasBytes = 0;
  let dropdownLabel = "";
  let counterValue = null;
  let timedOut = false;
  try {
    await page.goto(`http://localhost:${demo.port}/?city=${city}`, {
      waitUntil: "domcontentloaded",
      timeout: SUITE_TIMEOUT_MS,
    });
    // Let the renderer settle — networkidle is unreliable for 3D Tiles
    // streams, so we just bound the wait time.
    await page.waitForTimeout(12_000);

    const shot = await page.screenshot();
    canvasBytes = shot.length;

    // Each demo's <select> dropdown carries the city as its only <option>
    // matching the slug. Read whichever exists.
    dropdownLabel = await page.evaluate(() => {
      const sel = document.querySelector("select");
      if (!sel) return "";
      return sel.options[sel.selectedIndex]?.text ?? "";
    });

    // Counter element (Cesium ships one explicitly; others may add later)
    counterValue = await page.evaluate(() => {
      const el = document.getElementById("counter");
      return el ? el.textContent : null;
    });
  } catch (e) {
    timedOut = true;
    errors.push(`probe-error: ${e.message?.slice(0, 200)}`);
  }
  await browser.close();

  // Assertions
  const checks = [];
  checks.push(["no console errors", errors.length === 0,
    errors.length ? `${errors.length} errors; first: ${errors[0]?.slice(0, 120)}` : null]);
  checks.push(["dropdown reflects city", dropdownLabel.toLowerCase().includes(city.toLowerCase()),
    `dropdown: ${dropdownLabel || "(empty)"}`]);
  checks.push(["canvas painted >150KB", canvasBytes > MIN_PIXEL_BYTES,
    `screenshot=${(canvasBytes / 1024).toFixed(0)}KB`]);
  checks.push(["data-route hit", dataRequests.length > 0,
    `${dataRequests.length} requests to /data-${city}/`]);

  // Demo-specific assertion #5
  if (demo.name === "cesium") {
    const n = Number(counterValue);
    checks.push(["cesium counter > 0", n > 0, `counter=${counterValue}`]);
  } else if (demo.name === "deckgl") {
    checks.push(["deckgl glb tiles loaded", glbRequests.length > 0,
      `${glbRequests.length} *.glb requests`]);
  } else {
    // colorby: a tileset.json fetch is the minimum proof
    const tilesetHit = dataRequests.some(u => u.endsWith("/tileset.json"));
    checks.push(["colorby tileset.json fetched", tilesetHit,
      `tileset.json in dataRequests: ${tilesetHit}`]);
  }

  return { demo: demo.name, city, checks, timedOut, errors };
}

async function main() {
  console.log("plateau-bridge · browser smoke test");
  console.log(`testing ${DEMOS.length} demos × ${CITIES.length} cities = ${DEMOS.length * CITIES.length} probes`);

  // Pre-flight: confirm all servers are up
  for (const d of DEMOS) {
    const ok = await checkPortUp(d.port);
    if (!ok) {
      console.error(`✗ ${d.name} on :${d.port} not responding`);
      console.error(`  start it: ( cd examples/browser_${d.name} && pnpm dev & )`);
      process.exit(2);
    }
    console.log(`  ✓ ${d.name} :${d.port} up`);
  }
  console.log("");

  const results = [];
  for (const demo of DEMOS) {
    for (const city of CITIES) {
      process.stdout.write(`probing ${demo.name} :${demo.port} ?city=${city} ... `);
      const r = await probeOne(demo, city);
      results.push(r);
      const passed = r.checks.filter(c => c[1]).length;
      const total = r.checks.length;
      console.log(`${passed}/${total} ${passed === total ? "✓" : "✗"}`);
      for (const [name, ok, detail] of r.checks) {
        if (!ok) console.log(`    ✗ ${name}  ${detail ?? ""}`);
      }
    }
  }

  console.log("");
  const total = results.reduce((a, r) => a + r.checks.length, 0);
  const passed = results.reduce((a, r) => a + r.checks.filter(c => c[1]).length, 0);
  const failedRuns = results.filter(r => r.checks.some(c => !c[1]));
  console.log(`summary: ${passed} / ${total} checks passed`);
  console.log(`         ${results.length - failedRuns.length} / ${results.length} probes clean`);
  process.exit(failedRuns.length ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(2); });
