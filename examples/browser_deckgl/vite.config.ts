import { defineConfig } from "vite";
import { resolve, join, extname } from "path";
import { existsSync, statSync, createReadStream } from "fs";

// Mount every built city at its own URL prefix so the demo can switch
// between Shibuya/Osaka/etc. with just a `?city=` query param + reload
// (no env-var dance, no vite restart). Each prefix maps to `<repo>/out_<slug>/`.
//
// Override the base location with PLATEAU_DATA_ROOT if your `out_*/`
// directories live outside the repo (e.g. on a faster disk).
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
  build: { target: "es2022" },
  optimizeDeps: { esbuildOptions: { target: "es2022" } },
  server: {
    fs: { allow: [".", DATA_ROOT] },
  },
  plugins: [
    {
      name: "plateau-data-serve",
      configureServer(server) {
        server.middlewares.use((req, res, next) => {
          // URL shape: `/data-<city>/<relative-path>`. Keep the raw path
          // (Style filenames embed literal `%2F` for encoded tile URIs;
          // decoding the whole path would collapse them into nested dirs.)
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
});
