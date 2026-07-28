import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";
import { resolve } from "path";

// 每个 widget 是独立单 HTML(iframe 加载),管理面板是单独 SPA。
// 通过 WIDGET 环境变量选择构建目标:npm run build --  或交叉构建。
const WIDGET = process.env.WIDGET || "workspace-setup";

const inputs: Record<string, string> = {
  "workspace-setup": resolve(__dirname, "widgets/workspace-setup.html"),
  "approval": resolve(__dirname, "widgets/approval.html"),
  "chat": resolve(__dirname, "widgets/chat.html"),
  "ask-user": resolve(__dirname, "widgets/ask-user.html"),
  "diff": resolve(__dirname, "widgets/diff.html"),
  "panel": resolve(__dirname, "panel/index.html"),
};

export default defineConfig({
  plugins: [react(), viteSingleFile()],
  build: {
    outDir: "dist",
    emptyOutDir: false,
    rollupOptions: { input: inputs[WIDGET] },
  },
  server: { port: 5173 },
});
