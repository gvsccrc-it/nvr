import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath, URL } from "node:url";
import { brotliCompressSync, constants, gzipSync } from "node:zlib";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

const OUT_DIR = fileURLToPath(new URL("../app/web_dist", import.meta.url));

/**
 * The fingerprinted files, compressed once here, at build time (#537): the server sends the Brotli or gzip copy to a browser that takes it
 * (app/api/web_interface.py), so it costs nothing while running. The main script is about 490 kB as built and about 150 kB compressed.
 */
function precompress(): Plugin {
  return {
    name: "nvr-precompress",
    apply: "build",
    closeBundle() {
      const assets = join(OUT_DIR, "assets");
      for (const name of readdirSync(assets)) {
        const path = join(assets, name);
        if (!/\.(js|css|svg|json)$/.test(name) || statSync(path).size < 1024) continue;
        const data = readFileSync(path);
        writeFileSync(`${path}.br`, brotliCompressSync(data, { params: { [constants.BROTLI_PARAM_QUALITY]: 11, [constants.BROTLI_PARAM_SIZE_HINT]: data.length } }));
        writeFileSync(`${path}.gz`, gzipSync(data, { level: 9 }));
      }
    },
  };
}

// Served by FastAPI at the site root (app/api/web_interface.py).
export default defineConfig({
  base: "/",
  plugins: [react(), precompress()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: {
    outDir: OUT_DIR,
    emptyOutDir: true,
    sourcemap: false,
    target: "es2022",
    // Every file the build makes, so the service worker can keep the lazily loaded pages too (public/sw.js).
    manifest: "assets/chunks.json",
  },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8280", ws: true },
      "/healthz": "http://127.0.0.1:8280",
    },
  },
});
