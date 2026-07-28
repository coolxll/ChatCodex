import type { ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";

declare global {
  interface Window {
    __chatcodexWidgetRoot?: Root;
  }
}

/** Reuse the root during Vite HMR; production widgets still mount exactly once. */
export function mountWidget(node: ReactNode): void {
  const container = document.getElementById("root");
  if (!container) throw new Error("Widget root element is missing");
  const root = window.__chatcodexWidgetRoot ?? createRoot(container);
  window.__chatcodexWidgetRoot = root;
  root.render(node);
}
