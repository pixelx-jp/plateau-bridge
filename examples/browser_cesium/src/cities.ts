/**
 * City catalog + URL-driven dataset switching.
 *
 * Each demo serves data over a per-city URL prefix `/data-<city>/...` that
 * the vite middleware maps to the matching `out_<city>/` directory on
 * disk. The page reads `?city=<slug>` from window.location, defaults to
 * Shibuya, and feeds the resulting `DATA_BASE` + camera lat/lon into the
 * renderer.
 *
 * Switching is a full page reload (cheapest possible — tilesets are
 * gigabyte-scale and re-loading from scratch beats trying to dispose +
 * swap in-place). The dropdown handler just rewrites the URL.
 */

export type City = {
  slug: string;
  label: string;
  /** Camera lat/lon for the initial fly-to. Centroid of the built parquet. */
  lon: number;
  lat: number;
  buildings: number;
};

// IMPORTANT — no hardcoded ground-baseline parameter here.
//
// Older versions stored a per-city ``yBase`` constant (nusamai's local-Y
// origin varies: Sapporo +250 m, Nagoya −41 m, Shibuya −16 m) but that
// turned every new city into a manual audit step. The renderer now
// detects the baseline at runtime from the first few loaded meshes' Y
// minima (see ``yBaseUniform`` in main.ts) — adding a city becomes a
// catalogue entry + ``plateau build``, nothing else.
export const CITIES: City[] = [
// >>> cities-autogen >>>
  // Tokyo 23 wards
  { slug: "chiyoda", label: "Chiyoda 千代田区", lon: 139.7547, lat: 35.6877, buildings: 12548 },
  { slug: "chuo", label: "Chuo 中央区", lon: 139.7772, lat: 35.6699, buildings: 16884 },
  { slug: "minato", label: "Minato 港区", lon: 139.7398, lat: 35.6516, buildings: 32131 },
  { slug: "shinjuku", label: "Shinjuku 新宿区", lon: 139.7090, lat: 35.7010, buildings: 57485 },
  { slug: "bunkyo", label: "Bunkyo 文京区", lon: 139.7472, lat: 35.7175, buildings: 39576 },
  { slug: "taito", label: "Taito 台東区", lon: 139.7859, lat: 35.7156, buildings: 41451 },
  { slug: "sumida", label: "Sumida 墨田区", lon: 139.8154, lat: 35.7122, buildings: 52945 },
  { slug: "koto", label: "Koto 江東区", lon: 139.8143, lat: 35.6595, buildings: 65401 },
  { slug: "shinagawa", label: "Shinagawa 品川区", lon: 139.7337, lat: 35.6096, buildings: 68126 },
  { slug: "meguro", label: "Meguro 目黒区", lon: 139.6883, lat: 35.6300, buildings: 55398 },
  { slug: "ota", label: "Ota 大田区", lon: 139.7346, lat: 35.5666, buildings: 156655 },
  { slug: "setagaya", label: "Setagaya 世田谷区", lon: 139.6351, lat: 35.6397, buildings: 204700 },
  { slug: "shibuya", label: "Shibuya 渋谷区", lon: 139.6963, lat: 35.6678, buildings: 41858 },
  { slug: "nakano", label: "Nakano 中野区", lon: 139.6624, lat: 35.7110, buildings: 73037 },
  { slug: "suginami", label: "Suginami 杉並区", lon: 139.6255, lat: 35.6968, buildings: 143465 },
  { slug: "toshima", label: "Toshima 豊島区", lon: 139.7115, lat: 35.7315, buildings: 57788 },
  { slug: "kita", label: "Kita 北区", lon: 139.7290, lat: 35.7657, buildings: 73316 },
  { slug: "arakawa", label: "Arakawa 荒川区", lon: 139.7813, lat: 35.7400, buildings: 44403 },
  { slug: "itabashi", label: "Itabashi 板橋区", lon: 139.6765, lat: 35.7727, buildings: 106769 },
  { slug: "nerima", label: "Nerima 練馬区", lon: 139.6175, lat: 35.7479, buildings: 177032 },
  { slug: "adachi", label: "Adachi 足立区", lon: 139.7950, lat: 35.7789, buildings: 167103 },
  { slug: "katsushika", label: "Katsushika 葛飾区", lon: 139.8556, lat: 35.7533, buildings: 118551 },
  { slug: "edogawa", label: "Edogawa 江戸川区", lon: 139.8757, lat: 35.6925, buildings: 145332 },
  // Greater Tokyo + other regions
  { slug: "yokohama", label: "Yokohama 横浜市", lon: 139.5775, lat: 35.4601, buildings: 882831 },
  { slug: "kamakura", label: "Kamakura 鎌倉市", lon: 139.5380, lat: 35.3300, buildings: 69111 },
  { slug: "nagoya", label: "Nagoya 名古屋市", lon: 136.9243, lat: 35.1443, buildings: 736866 },
  { slug: "osaka", label: "Osaka 大阪市", lon: 135.4973, lat: 34.6696, buildings: 615513 },
  { slug: "fukuoka", label: "Fukuoka 福岡市", lon: 130.3581, lat: 33.5686, buildings: 355388 },
  { slug: "sapporo", label: "Sapporo 札幌市", lon: 141.2495, lat: 42.9947, buildings: 646431 },
// <<< cities-autogen <<<
];

export function activeCity(): City {
  const slug = new URLSearchParams(window.location.search).get("city") ?? "shibuya";
  return CITIES.find((c) => c.slug === slug) ?? CITIES[0];
}

/**
 * Render a small dropdown into the host element. On change, reload the
 * page with the new `?city=` param so vite + the renderer start fresh.
 */
export function mountCityPicker(host: HTMLElement, current: City): void {
  const wrap = document.createElement("div");
  wrap.style.cssText = "margin-top:8px;font-size:12px;line-height:1.4;color:#9ca3af;pointer-events:auto;";
  wrap.innerHTML = `<label>city &nbsp;</label>`;
  const sel = document.createElement("select");
  sel.style.cssText =
    "background:#1f2937;color:#e7e9ee;border:1px solid #374151;border-radius:4px;" +
    "padding:2px 6px;font-family:inherit;font-size:12px;cursor:pointer;";
  for (const c of CITIES) {
    const opt = document.createElement("option");
    opt.value = c.slug;
    opt.textContent = `${c.label} (${c.buildings.toLocaleString()})`;
    if (c.slug === current.slug) opt.selected = true;
    sel.appendChild(opt);
  }
  sel.addEventListener("change", () => {
    const u = new URL(window.location.href);
    u.searchParams.set("city", sel.value);
    window.location.href = u.toString();
  });
  wrap.appendChild(sel);
  host.appendChild(wrap);
}
