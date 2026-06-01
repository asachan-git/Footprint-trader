import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3001,
    strictPort: true,
    proxy: {
      "/dashboard/state":  "http://localhost:5000",
      "/dashboard/stream": { target: "http://localhost:5000", changeOrigin: true, ws: false },
    },
  },
  build: {
    outDir: "dist",
  },
});
