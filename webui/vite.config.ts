import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  plugins: [vue()],
  base: "/assets/",
  build: {
    outDir: resolve(root, "../anima/webui/static"),
    emptyOutDir: true,
    assetsDir: ".",
    sourcemap: false,
    rollupOptions: {
      output: {
        entryFileNames: "app-[hash].js",
        chunkFileNames: "chunk-[hash].js",
        assetFileNames: "app-[hash][extname]"
      }
    }
  },
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8088" }
  }
});
