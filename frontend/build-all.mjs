// 全量构建:widget 各目标 + 规整产物到 dist/ 平铺(widget)与 dist/panel/(面板)。
import { execSync } from "node:child_process";
import { copyFileSync, rmSync } from "node:fs";

// Keep every ChatGPT component template in sync with backend/app/widgets.py.
const widgets = ["workspace-setup", "chat", "ask-user", "approval", "diff"];
rmSync("dist", { recursive: true, force: true });
for (const w of widgets) {
  console.log(`\n=== build ${w} ===`);
  execSync(`vite build`, { stdio: "inherit", env: { ...process.env, WIDGET: w } });
  copyFileSync(`dist/widgets/${w}.html`, `dist/${w}.html`);
}
console.log(`\n=== build panel ===`);
execSync(`vite build`, { stdio: "inherit", env: { ...process.env, WIDGET: "panel" } });
console.log("\nDone.");
