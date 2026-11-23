import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The hosted test site (invite-link sign-in), served by the chat service itself (CHAT_WEB_DIR).
// `npm run build:pilot` → dist-pilot/ (the Dockerfile copies it to /app/web).
// `npm run dev:pilot` → http://localhost:5174, API proxied to the chat service on :8090 (CHAT_AUTH_PROVIDER=invite).
export default defineConfig({
  root: "pilot",
  base: "/",
  plugins: [react()],
  server: { port: 5174, proxy: { "/chat": "http://localhost:8090" } },
  build: { outDir: "../dist-pilot", emptyOutDir: true },
});
