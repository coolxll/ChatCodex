import React from "react";
import {
  hostViewMode,
  type DisplayMode,
  type HostView,
} from "../lib/openai";

export type WidgetSurface = "inline" | "modal" | "fullscreen";

export function resolveSurface(
  displayMode: DisplayMode,
  view?: HostView,
  requested?: WidgetSurface,
): WidgetSurface {
  if (requested) return requested;
  const mode = hostViewMode(view);
  if (mode === "modal") return "modal";
  if (mode === "fullscreen") return "fullscreen";
  return displayMode === "fullscreen" ? "fullscreen" : "inline";
}

export function WidgetShell({
  surface,
  className = "",
  children,
}: React.PropsWithChildren<{ surface: WidgetSurface; className?: string }>) {
  return (
    <main
      className={`chatcodex-widget widget-shell ${className}`.trim()}
      data-surface={surface}
      data-display-mode={surface === "fullscreen" ? "fullscreen" : "inline"}
    >
      {children}
    </main>
  );
}
