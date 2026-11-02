import { fileURLToPath } from "node:url";
import { defineConfig, searchForWorkspaceRoot } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/inferdrome/dashboard/static",
    emptyOutDir: true,
    // Keep this small public download inside the JS bundle; the package gate
    // inventories external assets through HTML and CSS references.
    assetsInlineLimit: (filePath) => filePath.endsWith("/docs/results/recovery-headroom-2026-10-19.json") ? true : undefined,
  },
  server: {
    host: "127.0.0.1",
    fs: {
      allow: [
        searchForWorkspaceRoot(fileURLToPath(new URL(".", import.meta.url))),
        // Share curated public results with docs without exposing private captures.
        fileURLToPath(new URL("../docs/results", import.meta.url)),
      ],
    },
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8787",
        changeOrigin: false,
      },
    },
  },
  preview: {
    host: "127.0.0.1",
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    include: ["src/**/*.test.{ts,tsx}"],
    css: true,
  },
});
