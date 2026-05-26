import { defineConfig } from "vite";
import cesium from "vite-plugin-cesium";
import { resolve, join, extname } from "node:path";
import { existsSync, statSync, createReadStream } from "node:fs";

// Per-city data routing — see browser_colorby/vite.config.ts for the
// full rationale. Each `/data-<slug>/*` URL maps to `out_<slug>/*` on
// disk, so the demo can switch cities via `?city=osaka` without an env
// var + restart.
const DATA_ROOT = process.env.PLATEAU_DATA_ROOT
  ? resolve(process.env.PLATEAU_DATA_ROOT)
  : resolve(__dirname, "../..");

const MIME: Record<string, string> = {
  ".json":    "application/json",
  ".arrow":   "application/vnd.apache.arrow.file",
  ".pmtiles": "application/octet-stream",
  ".fgb":     "application/octet-stream",
  ".glb":     "model/gltf-binary",
  ".parquet": "application/octet-stream",
};

const DATA_PREFIX = /^\/data-([a-z_]+)\/(.*)$/;

export default defineConfig({
  publicDir: false,
  plugins: [
    cesium(),
    {
      name: "plateau-data-serve",
      configureServer(server) {
        server.middlewares.use((req, res, next) => {
          const raw = (req.url ?? "/").split("?")[0];
          const m = raw.match(DATA_PREFIX);
          if (!m) { next(); return; }
          const [, city, rel] = m;
          const filePath = join(DATA_ROOT, `out_${city}`, rel);
          if (!existsSync(filePath) || !statSync(filePath).isFile()) {
            next();
            return;
          }
          res.setHeader("Content-Type", MIME[extname(filePath)] ?? "application/octet-stream");
          res.setHeader("Access-Control-Allow-Origin", "*");
          res.setHeader("Accept-Ranges", "bytes");
          createReadStream(filePath).pipe(res);
        });
      },
    },
  ],
  build: { target: "es2022" },
  optimizeDeps: { esbuildOptions: { target: "es2022" } },
  server: {
    fs: { allow: [".", DATA_ROOT] },
  },
});
