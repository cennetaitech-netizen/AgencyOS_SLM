/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev`: demo app at http://localhost:5173, API proxied to the chat service on :8090.
// `npm run build`: library bundle in dist/ (React is a peer dependency, not bundled).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": { target: "http://localhost:8090", rewrite: (p) => p.replace(/^\/api/, "") } },
  },
  build: {
    lib: { entry: "src/index.ts", formats: ["es"], fileName: () => "agencyos-assistant.js" },
    rollupOptions: { external: ["react", "react-dom", "react/jsx-runtime"] },
    cssCodeSplit: false,
  },
  test: { environment: "jsdom", setupFiles: ["test/setup.ts"], css: false },
});
