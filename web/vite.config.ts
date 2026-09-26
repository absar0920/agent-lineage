import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api/lineage": { target: "http://localhost:8002", rewrite: (p) => p.replace(/^\/api\/lineage/, "") },
      "/api/agent": { target: "http://localhost:8001", rewrite: (p) => p.replace(/^\/api\/agent/, "") },
    },
  },
  build: { chunkSizeWarningLimit: 900 },
});
