export type PrivateCapabilityName =
  | "toast"
  | "haptic"
  | "prompt"
  | "targetedReply"
  | "focusedObject"
  | "conversationOverlay"
  | "rawMcp";

export type PrivateCapabilities = Record<PrivateCapabilityName, boolean>;

export interface PrivateOpenAiHost {
  showToast?(args: {
    level: "success" | "warning" | "danger";
    title: string;
    body?: string;
  }): unknown;
  triggerHaptic?(args: {
    type: "light" | "medium" | "heavy" | "selection" | "success" | "warning" | "error";
  }): unknown;
  requestPrompt?(args: { placeholder?: string }): Promise<{ prompt: string } | null | undefined>;
  openPromptInput?(args: {
    placeholder?: string;
    clientX: number;
    clientY: number;
  }): Promise<{ prompt: string } | null | undefined>;
  requestTargetedReply?(args: { text: string }): unknown;
  requestFocusedObject?(args: { title: string; params: Record<string, unknown> }): unknown;
  requestCloseFocusedObject?(): unknown;
  openConversationOverlay?(args: {
    conversationId: string;
    origin?: string;
    title?: string;
  }): unknown;
  callMcp?(args: { method: string; params?: Record<string, unknown> }): Promise<unknown>;
}

declare global {
  interface Window {
    __CHATCODEX_PRIVATE_API_DISABLED__?: PrivateCapabilityName[] | string | boolean;
  }
}

const runtimeDisabled = new Set<PrivateCapabilityName>();
const PRIVATE_CAPABILITIES_EVENT = "chatcodex:private-capabilities";

function configuredDisabled(): Set<string> {
  if (typeof window === "undefined") return new Set();
  const configured = window.__CHATCODEX_PRIVATE_API_DISABLED__;
  if (configured === true) return new Set(["*"]);
  if (Array.isArray(configured)) return new Set(configured);
  if (typeof configured === "string") {
    return new Set(configured.split(",").map((item) => item.trim()).filter(Boolean));
  }
  return new Set();
}

export function privateHost(): PrivateOpenAiHost | undefined {
  if (typeof window === "undefined") return undefined;
  return window.openai as unknown as PrivateOpenAiHost | undefined;
}

export function disablePrivateCapability(name: PrivateCapabilityName): void {
  runtimeDisabled.add(name);
  if (typeof window !== "undefined" && typeof window.dispatchEvent === "function") {
    window.dispatchEvent(new CustomEvent(PRIVATE_CAPABILITIES_EVENT));
  }
}

export function resetPrivateCapabilityFailures(): void {
  runtimeDisabled.clear();
  if (typeof window !== "undefined" && typeof window.dispatchEvent === "function") {
    window.dispatchEvent(new CustomEvent(PRIVATE_CAPABILITIES_EVENT));
  }
}

export function onPrivateCapabilitiesChanged(handler: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  window.addEventListener(PRIVATE_CAPABILITIES_EVENT, handler);
  return () => window.removeEventListener(PRIVATE_CAPABILITIES_EVENT, handler);
}

export function isPrivateCapabilityDisabled(name: PrivateCapabilityName): boolean {
  const configured = configuredDisabled();
  return runtimeDisabled.has(name) ||
    configured.has(name) ||
    configured.has("*") ||
    configured.has("all");
}

function hasCallableHostMethod(
  host: PrivateOpenAiHost | undefined,
  name: keyof PrivateOpenAiHost,
): boolean {
  if (!host) return false;
  try {
    return Reflect.has(host, name) && typeof Reflect.get(host, name) === "function";
  } catch {
    return false;
  }
}

export function detectPrivateCapabilities(): PrivateCapabilities {
  const host = privateHost();
  const enabled = (name: PrivateCapabilityName, present: boolean) =>
    present && !isPrivateCapabilityDisabled(name);
  return {
    toast: enabled("toast", hasCallableHostMethod(host, "showToast")),
    haptic: enabled("haptic", hasCallableHostMethod(host, "triggerHaptic")),
    prompt: enabled(
      "prompt",
      hasCallableHostMethod(host, "requestPrompt") ||
        hasCallableHostMethod(host, "openPromptInput"),
    ),
    targetedReply: enabled(
      "targetedReply",
      hasCallableHostMethod(host, "requestTargetedReply"),
    ),
    focusedObject: enabled(
      "focusedObject",
      hasCallableHostMethod(host, "requestFocusedObject") &&
        hasCallableHostMethod(host, "requestCloseFocusedObject"),
    ),
    conversationOverlay: enabled(
      "conversationOverlay",
      hasCallableHostMethod(host, "openConversationOverlay"),
    ),
    rawMcp: enabled("rawMcp", hasCallableHostMethod(host, "callMcp")),
  };
}
