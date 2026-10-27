import { defineConfig } from "vite";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  root: "src",
  // Relative, not root-absolute, asset paths — Tauri's internal protocol
  // tolerated "/assets/..." but Electron loads the built HTML via a plain
  // file:// URL, where a root-absolute path resolves to the filesystem
  // root instead of dist/. "./assets/..." works under both.
  base: "./",
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    rollupOptions: {
      // Two entry points now: index.html (Electron) and mobile.html (the
      // PWA, deployed to the server for the phone) — without listing both
      // explicitly, Vite only builds the implicit default (index.html)
      // and mobile.html would silently never make it into dist/.
      input: {
        main: resolve(__dirname, "src/index.html"),
        mobile: resolve(__dirname, "src/mobile.html"),
      },
    },
  },
  server: {
    port: 1420,
    strictPort: true,
  },
  clearScreen: false,
});
