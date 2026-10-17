import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base: "./" 让生产构建可被 Electron 以 file:// 加载
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: { port: 5173, strictPort: true },
});
