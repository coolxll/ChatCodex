/** ChatGPT Apps SDK bridge with a standards-based MCP Apps fallback. */

export type DisplayMode = "inline" | "pip" | "fullscreen";
export type Theme = "light" | "dark";
export type HostViewMode = "inline" | "modal" | "fullscreen" | string;
export interface HostViewContext {
  mode?: HostViewMode;
  params?: Record<string, unknown> | null;
  isTombstone?: boolean;
}
export type HostView = HostViewMode | HostViewContext;

export interface HostCapabilities {
  modal: boolean;
  fullscreen: boolean;
  files: boolean;
}

export interface HostContext {
  theme: Theme;
  displayMode: DisplayMode;
  maxHeight?: number;
  locale: string;
  safeArea: { top: number; right: number; bottom: number; left: number };
  view?: HostView;
  userAgent?: string;
  capabilities: HostCapabilities;
}

export interface OpenAiGlobals {
  toolInput?: any;
  toolOutput?: any;
  toolResponseMetadata?: any;
  widgetState?: any;
  theme?: Theme;
  displayMode?: DisplayMode;
  maxHeight?: number;
  locale?: string;
  safeArea?: { insets: { top: number; bottom: number; left: number; right: number } };
  safeAreaInsets?: { top: number; bottom: number; left: number; right: number };
  availableDisplayModes?: DisplayMode[];
  containerDimensions?: { width?: number; height?: number; maxWidth?: number; maxHeight?: number };
  styles?: { variables?: Record<string, string | undefined>; css?: { fonts?: string } };
  userAgent?: string;
  view?: HostView;

  callTool(name: string, args?: Record<string, any>): Promise<any>;
  sendFollowUpMessage(args: { prompt: string; scrollToBottom?: boolean }): Promise<void>;
  setWidgetState(state: any): void;
  requestDisplayMode(args: { mode: DisplayMode }): Promise<{ mode: DisplayMode }>;
  requestModal(args?: { template?: string; params?: any }): Promise<unknown>;
  requestClose(): Promise<void>;
  notifyIntrinsicHeight(height?: number): void;
  openExternal(args: { href: string; redirectUrl?: string | false }): void;
  uploadFile?(file: File, opts?: { library?: boolean }): Promise<{ fileId: string }>;
  selectFiles?(): Promise<Array<{ fileId: string; fileName: string; mimeType: string }>>;
  setOpenInAppUrl?(args: { href: string }): void;
}

declare global {
  interface Window {
    openai?: OpenAiGlobals;
  }
}

type RpcPending = { resolve(value: any): void; reject(reason: Error): void; timer: number };
const pending = new Map<number, RpcPending>();
let nextId = 1;
let standardInitialized: Promise<void> | null = null;
let standardUnsupported = false;
const standardGlobals: Partial<OpenAiGlobals> = {};
/** 内部事件:标准桥 initialize 完成、hostContext 首次可用时派发。 */
const HOST_CONTEXT_EVENT = "chatcodex:host-context";
/** ChatGPT 沙箱以下发 DOM CustomEvent 的方式更新 window.openai 全局(非 postMessage)。 */
const SET_GLOBALS_EVENTS = ["openai:set_globals", "webplus:set_globals"];

const parentOrigin = (() => {
  try { return document.referrer ? new URL(document.referrer).origin : "*"; }
  catch { return "*"; }
})();

/**
 * ChatGPT 把 theme/displayMode/toolInput 等直接铺到 window.openai,而 styles、
 * safeAreaInsets、containerDimensions 等只经 MCP Apps 标准桥的 hostContext 下发。
 * 两条通道合并读取,window.openai 优先,但其 undefined 键不得遮蔽标准桥已收到的值。
 */
export const openai = (): OpenAiGlobals | undefined => {
  if (typeof window === "undefined") return undefined;
  const host = window.openai;
  if (!host) return standardGlobals as OpenAiGlobals;
  const merged: Record<string, any> = { ...standardGlobals };
  for (const [key, value] of Object.entries(host)) {
    if (value !== undefined) merged[key] = value;
  }
  return merged as OpenAiGlobals;
};

export const unwrapToolResult = <T = any>(value: any): T =>
  (value?.structuredContent ?? value?.structured_content ?? value) as T;

export const toolInput = <T = any>(): T | null => (openai()?.toolInput ?? standardGlobals.toolInput ?? null) as T | null;
export const toolOutput = <T = any>(): T | null => unwrapToolResult<T>(openai()?.toolOutput ?? standardGlobals.toolOutput) ?? null;
export const widgetState = <T = any>(): T | null => (openai()?.widgetState ?? standardGlobals.widgetState ?? null) as T | null;
export const theme = (): Theme => openai()?.theme ?? standardGlobals.theme ?? "light";
export const displayMode = (): DisplayMode => openai()?.displayMode ?? "inline";

export function hostViewMode(view: HostView | undefined): HostViewMode | undefined {
  return typeof view === "string" ? view : view?.mode;
}

export function hostViewParams<T extends Record<string, unknown> = Record<string, unknown>>(
  view: HostView | undefined,
): T | null {
  if (!view || typeof view === "string" || !view.params) return null;
  return view.params as T;
}

export function hostViewIsTombstone(view: HostView | undefined): boolean {
  return typeof view === "object" && Boolean(view?.isTombstone);
}

export function hostCapabilities(): HostCapabilities {
  const oa = openai();
  const available = oa?.availableDisplayModes ?? standardGlobals.availableDisplayModes;
  return {
    modal: typeof window.openai?.requestModal === "function",
    fullscreen: available?.includes("fullscreen") ??
      typeof window.openai?.requestDisplayMode === "function",
    files: typeof oa?.uploadFile === "function" || typeof oa?.selectFiles === "function",
  };
}

export function hostContext(): HostContext {
  const oa = openai();
  const insets = oa?.safeAreaInsets ?? oa?.safeArea?.insets;
  return {
    theme: theme(),
    displayMode: displayMode(),
    maxHeight: oa?.maxHeight ?? oa?.containerDimensions?.maxHeight,
    locale: oa?.locale ?? "zh-CN",
    safeArea: {
      top: insets?.top ?? 0,
      right: insets?.right ?? 0,
      bottom: insets?.bottom ?? 0,
      left: insets?.left ?? 0,
    },
    view: oa?.view,
    userAgent: oa?.userAgent,
    capabilities: hostCapabilities(),
  };
}

function rpc(method: string, params: Record<string, any>, timeoutMs = 15000): Promise<any> {
  const id = nextId++;
  const promise = new Promise<any>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      if (!pending.delete(id)) return;
      reject(new Error(`Host bridge timed out: ${method}`));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
  });
  window.parent.postMessage({ jsonrpc: "2.0", id, method, params }, parentOrigin);
  return promise;
}

async function ensureStandardBridge(): Promise<void> {
  if (standardUnsupported) throw new Error("This host does not expose the standard MCP Apps bridge");
  if (!standardInitialized) {
    standardInitialized = rpc("ui/initialize", {
      protocolVersion: "2026-01-26",
      appInfo: { name: "ChatCodex Widget", version: "0.1.0" },
      appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
    }, window.openai ? 5000 : 15000).then((result) => {
      Object.assign(standardGlobals, result?.hostContext ?? {});
      window.parent.postMessage({
        jsonrpc: "2.0", method: "ui/notifications/initialized", params: {},
      }, parentOrigin);
      // styles 等 hostContext 只随 initialize 结果下发一次,主动通知 hooks 重取。
      window.dispatchEvent(new CustomEvent(HOST_CONTEXT_EVENT));
    }).catch((error) => {
      standardInitialized = null;
      if (window.openai) standardUnsupported = true;
      throw error;
    });
  }
  return standardInitialized;
}

export async function callTool<T = any>(name: string, args?: Record<string, any>): Promise<T> {
  if (window.openai?.callTool) {
    return unwrapToolResult<T>(await window.openai.callTool(name, args));
  }
  await ensureStandardBridge();
  return unwrapToolResult<T>(await rpc("tools/call", { name, arguments: args ?? {} }));
}

export async function sendFollowUpMessage(prompt: string): Promise<void> {
  const send = window.openai?.sendFollowUpMessage;
  if (send) {
    await send({ prompt });
    return;
  }
  await ensureStandardBridge();
  await rpc("ui/message", {
    role: "user",
    content: [{ type: "text", text: prompt }],
  });
}

export function setWidgetState(state: any): void {
  standardGlobals.widgetState = state;
  // ChatGPT widgetState is UI-only. ui/update-model-context is model-visible
  // and therefore is not a semantics-preserving fallback for persistence.
  window.openai?.setWidgetState?.(state);
}

export async function requestModal(template?: string, params?: any): Promise<boolean> {
  const modal = window.openai?.requestModal;
  if (!modal) return false;
  await modal(template ? { template, params } : {});
  return true;
}

/** Public-only navigation fallback. Private APIs never call this helper. */
export async function requestModalOrFullscreen(
  template: string,
  params?: any,
): Promise<"modal" | "fullscreen" | "unavailable"> {
  try {
    if (await requestModal(template, params)) return "modal";
  } catch {
    // A rejected public modal request may still allow fullscreen.
  }
  try {
    const mode = await requestDisplayMode("fullscreen");
    return mode === "fullscreen" ? "fullscreen" : "unavailable";
  } catch {
    return "unavailable";
  }
}

export async function requestDisplayMode(mode: DisplayMode): Promise<DisplayMode> {
  const oa = openai();
  const available = oa?.availableDisplayModes ?? standardGlobals.availableDisplayModes;
  if (available?.length && !available.includes(mode)) return displayMode();
  let result: { mode: DisplayMode };
  if (window.openai?.requestDisplayMode) {
    try {
      result = await window.openai.requestDisplayMode({ mode });
    } catch {
      await ensureStandardBridge();
      result = await rpc("ui/request-display-mode", { mode });
    }
  } else {
    await ensureStandardBridge();
    result = await rpc("ui/request-display-mode", { mode });
  }
  standardGlobals.displayMode = result?.mode ?? standardGlobals.displayMode;
  if (window.openai && result?.mode) window.openai.displayMode = result.mode;
  window.dispatchEvent(new CustomEvent(HOST_CONTEXT_EVENT));
  return result?.mode ?? displayMode();
}

export async function requestClose(): Promise<boolean> {
  const close = openai()?.requestClose;
  if (!close) return false;
  await close();
  return true;
}

let lastReportedSize = "";
export function notifyIntrinsicHeight(height?: number): void {
  const value = height ?? document.body.scrollHeight;
  const width = document.body.scrollWidth;
  const key = `${width}x${value}`;
  if (key === lastReportedSize) return;
  lastReportedSize = key;
  if (window.openai?.notifyIntrinsicHeight) {
    try {
      window.openai.notifyIntrinsicHeight(value);
      return;
    } catch {
      // Continue through the standard MCP Apps size notification.
    }
  }
  void ensureStandardBridge().then(() => {
    window.parent.postMessage({
      jsonrpc: "2.0", method: "ui/notifications/size-changed",
      params: { width, height: value },
    }, parentOrigin);
  }).catch(() => {});
}

export async function openExternal(href: string): Promise<void> {
  if (window.openai?.openExternal) {
    window.openai.openExternal({ href });
    return;
  }
  await ensureStandardBridge();
  await rpc("ui/open-link", { url: href });
}

/** Subscribe to trusted host bridge notifications and JSON-RPC responses. */
export function onOpenAiEvent(handler: (ev: { type: string; detail?: any }) => void): () => void {
  const listener = (e: MessageEvent) => {
    if (e.source !== window.parent) return;
    if (parentOrigin !== "*" && e.origin !== parentOrigin) return;
    const d = e.data;
    if (!d || typeof d !== "object") return;

    if (d.jsonrpc === "2.0" && d.id != null && ("result" in d || "error" in d)) {
      const waiter = pending.get(Number(d.id));
      if (!waiter) return;
      pending.delete(Number(d.id));
      window.clearTimeout(waiter.timer);
      if (d.error) waiter.reject(new Error(d.error.message ?? "Host bridge request failed"));
      else waiter.resolve(d.result);
      return;
    }

    const type = String(d.type ?? d.method ?? "");
    if (!(type === "openai:set_globals" || type.startsWith("ui/"))) return;
    const detail = d.detail ?? d.params ?? {};
    if (type.includes("host-context")) Object.assign(standardGlobals, detail.hostContext ?? detail);
    if (type.includes("display-mode")) {
      standardGlobals.displayMode = detail.mode ?? detail.displayMode ?? standardGlobals.displayMode;
    }
    if (type.includes("tool-input")) standardGlobals.toolInput = detail.arguments ?? detail;
    if (type.includes("tool-result")) standardGlobals.toolOutput = unwrapToolResult(detail.result ?? detail);
    if (type === "openai:set_globals" && window.openai) {
      Object.assign(window.openai, detail.globals ?? detail);
    }
    handler({ type, detail });
  };
  // ChatGPT 沙箱直接 dispatch CustomEvent(window.openai 已被它更新),而非 postMessage。
  const globalsListener = (e: Event) => {
    const globals = (e as CustomEvent).detail?.globals ?? {};
    handler({ type: "openai:set_globals", detail: globals });
  };
  const hostContextListener = () => {
    handler({ type: "ui/notifications/host-context-changed", detail: standardGlobals });
  };
  window.addEventListener("message", listener);
  for (const type of SET_GLOBALS_EVENTS) window.addEventListener(type, globalsListener);
  window.addEventListener(HOST_CONTEXT_EVENT, hostContextListener);
  return () => {
    window.removeEventListener("message", listener);
    for (const type of SET_GLOBALS_EVENTS) window.removeEventListener(type, globalsListener);
    window.removeEventListener(HOST_CONTEXT_EVENT, hostContextListener);
  };
}
