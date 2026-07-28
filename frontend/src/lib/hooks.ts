import { useEffect, useState, useCallback } from "react";
import * as OA from "../lib/openai";
import {
  detectPrivateCapabilities,
  onPrivateCapabilitiesChanged,
  type PrivateCapabilities,
} from "../lib/private-capabilities";

/** 跟随宿主主题写入 data-theme,并随 theme 变化更新。 */
export function useTheme(): OA.Theme {
  const [t, setT] = useState<OA.Theme>(OA.theme());
  useEffect(() => {
    const apply = () => {
      const th = OA.theme();
      setT(th);
      document.documentElement.dataset.theme = th;
      document.documentElement.style.colorScheme = th;
      const globals = OA.openai();
      const locale = globals?.locale ?? "zh-CN";
      document.documentElement.lang = locale;
      document.documentElement.dir = /^(ar|fa|he|ur)(-|$)/i.test(locale) ? "rtl" : "ltr";
      const hostMaxHeight = globals?.maxHeight ?? globals?.containerDimensions?.maxHeight;
      if (typeof hostMaxHeight === "number" && Number.isFinite(hostMaxHeight) && hostMaxHeight > 0) {
        document.documentElement.style.setProperty("--host-max-height", `${hostMaxHeight}px`);
      } else {
        document.documentElement.style.removeProperty("--host-max-height");
      }
      const insets = globals?.safeAreaInsets ?? globals?.safeArea?.insets;
      if (insets) {
        for (const side of ["top", "right", "bottom", "left"] as const) {
          document.documentElement.style.setProperty(`--safe-${side}`, `${insets[side]}px`);
        }
      }
      for (const [name, value] of Object.entries(globals?.styles?.variables ?? {})) {
        if (name.startsWith("--") && value != null) {
          document.documentElement.style.setProperty(name, value);
        }
      }
      const fontCss = globals?.styles?.css?.fonts;
      let fontStyle = document.getElementById("mcp-host-fonts") as HTMLStyleElement | null;
      if (fontCss) {
        if (!fontStyle) {
          fontStyle = document.createElement("style");
          fontStyle.id = "mcp-host-fonts";
          document.head.appendChild(fontStyle);
        }
        fontStyle.textContent = fontCss;
      }
    };
    apply();
    const off = OA.onOpenAiEvent((ev) => {
      if (ev.type.includes("theme") || ev.type.includes("globals") ||
          ev.type.includes("host-context")) apply();
    });
    return off;
  }, []);
  return t;
}

/** Unified host context. Widgets should not maintain a second display-mode state. */
export function useHostContext(): OA.HostContext {
  const [context, setContext] = useState<OA.HostContext>(OA.hostContext());
  useEffect(() => {
    const apply = () => setContext(OA.hostContext());
    apply();
    return OA.onOpenAiEvent(apply);
  }, []);
  return context;
}

/** Private methods are detected independently and disappear after circuit breaking. */
export function usePrivateCapabilities(): PrivateCapabilities {
  const [capabilities, setCapabilities] = useState(detectPrivateCapabilities());
  useEffect(() => {
    const apply = () => setCapabilities(detectPrivateCapabilities());
    apply();
    const offHost = OA.onOpenAiEvent(apply);
    const offPrivate = onPrivateCapabilitiesChanged(apply);
    return () => {
      offHost();
      offPrivate();
    };
  }, []);
  return capabilities;
}

/** 读 toolInput(approval-gated 时可能为 null,宿主批准后经 tool-input 通知下发)。 */
export function useToolInput<T = any>(): T | null {
  const [input, setInput] = useState<T | null>(OA.toolInput<T>());
  useEffect(() => {
    setInput(OA.toolInput<T>());
    const off = OA.onOpenAiEvent((ev) => {
      if (ev.type.includes("tool-input") || ev.type === "openai:set_globals") {
        setInput((ev.detail?.arguments ?? ev.detail?.toolInput ?? OA.toolInput<T>()) as T | null);
      }
    });
    return off;
  }, []);
  return input;
}

/** 读 toolOutput,并随 tool-result 通知更新。 */
export function useToolOutput<T = any>(): T | null {
  const [out, setOut] = useState<T | null>(OA.toolOutput<T>());
  useEffect(() => {
    setOut(OA.toolOutput<T>());
    const off = OA.onOpenAiEvent((ev) => {
      if (ev.type.includes("tool-result") || ev.type === "openai:set_globals") {
        const result = ev.detail?.result ?? ev.detail?.toolOutput ?? OA.toolOutput<T>();
        setOut(OA.unwrapToolResult<T>(result));
      }
    });
    return off;
  }, []);
  return out;
}

/** 内容变化后上报高度,避免 iframe 滚动条。 */
export function useIntrinsicHeight(dep: any[] = []) {
  useEffect(() => {
    let frame = 0;
    const report = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => OA.notifyIntrinsicHeight());
    };
    report();
    const ro = new ResizeObserver(report);
    ro.observe(document.body);
    return () => { ro.disconnect(); window.cancelAnimationFrame(frame); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, dep);
}

export const useCallTool = () => useCallback(OA.callTool, []);
