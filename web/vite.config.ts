import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8080",
        changeOrigin: true,
        // 带上 X-Forwarded-For，本地联调时访客额度才能按真实浏览器 IP 计数
        xfwd: true,
      },
    },
  },
});
