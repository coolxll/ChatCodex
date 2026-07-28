import {
  detectPrivateCapabilities,
  disablePrivateCapability,
  isPrivateCapabilityDisabled,
  privateHost,
  type PrivateCapabilityName,
  type PrivateOpenAiHost,
} from "./private-capabilities.ts";
import {
  privateApiFailure,
  type PrivateApiResult,
} from "./private-api-errors.ts";

export type ToastInput = Parameters<NonNullable<PrivateOpenAiHost["showToast"]>>[0];
export type HapticType = Parameters<NonNullable<PrivateOpenAiHost["triggerHaptic"]>>[0]["type"];

const RAW_MCP_METHODS = new Set([
  "resources/list",
  "resources/read",
  "resources/subscribe",
  "resources/unsubscribe",
]);
const PRIVATE_API_TIMEOUT_MS = 8000;

function unavailable<T>(name: PrivateCapabilityName): PrivateApiResult<T> {
  return privateApiFailure(
    isPrivateCapabilityDisabled(name) ? "disabled" : "unavailable",
  );
}

function activationAvailable(): boolean {
  if (typeof navigator === "undefined" || !navigator.userActivation) return true;
  return navigator.userActivation.isActive;
}

function hostMethod<K extends keyof PrivateOpenAiHost>(
  host: PrivateOpenAiHost,
  name: K,
): NonNullable<PrivateOpenAiHost[K]> | undefined {
  if (!Reflect.has(host, name)) return undefined;
  const method = Reflect.get(host, name) as PrivateOpenAiHost[K];
  return typeof method === "function"
    ? method as NonNullable<PrivateOpenAiHost[K]>
    : undefined;
}

function promptAnchorCoordinates(): { clientX: number; clientY: number } {
  if (typeof document === "undefined") return { clientX: 0, clientY: 0 };
  try {
    const rect = document.activeElement?.getBoundingClientRect();
    return {
      clientX: Number.isFinite(rect?.left) ? rect?.left ?? 0 : 0,
      clientY: Number.isFinite(rect?.top) ? rect?.top ?? 0 : 0,
    };
  } catch {
    return { clientX: 0, clientY: 0 };
  }
}

async function invoke<T>(
  name: PrivateCapabilityName,
  call: (host: PrivateOpenAiHost) => T | Promise<T>,
  options: {
    userActivation?: boolean;
    validate?: (value: T) => boolean;
    undefinedRejectionIsCancellation?: boolean;
  } = {},
): Promise<PrivateApiResult<T>> {
  if (!detectPrivateCapabilities()[name]) return unavailable(name);
  if (options.userActivation && !activationAvailable()) {
    return privateApiFailure("user_activation_required");
  }
  const host = privateHost();
  if (!host) return unavailable(name);
  let timer: number | undefined;
  try {
    const timeout = new Promise<never>((_, reject) => {
      timer = globalThis.setTimeout(
        () => reject(new Error(`Private host capability timed out: ${name}`)),
        PRIVATE_API_TIMEOUT_MS,
      );
    });
    const value = await Promise.race([Promise.resolve(call(host)), timeout]);
    if (options.validate && !options.validate(value)) {
      disablePrivateCapability(name);
      return privateApiFailure("invalid_result");
    }
    return { ok: true, value };
  } catch (cause) {
    if (
      isCancellation(cause) ||
      (options.undefinedRejectionIsCancellation && cause == null)
    ) {
      return privateApiFailure("cancelled", cause);
    }
    disablePrivateCapability(name);
    return privateApiFailure("rejected", cause);
  } finally {
    if (timer !== undefined) globalThis.clearTimeout(timer);
  }
}

export function showToast(input: ToastInput): Promise<PrivateApiResult<void>> {
  return invoke("toast", (host) => {
    host.showToast!({ ...input, body: input.body ?? "" });
  });
}

export function triggerHaptic(type: HapticType): Promise<PrivateApiResult<void>> {
  return invoke(
    "haptic",
    (host) => {
      host.triggerHaptic!({ type });
    },
    { userActivation: true },
  );
}

export function requestPrompt(
  placeholder?: string,
): Promise<PrivateApiResult<string>> {
  return invoke<{ prompt: string } | null | undefined>(
    "prompt",
    (host) => {
      const request = hostMethod(host, "requestPrompt");
      if (request) return request.call(host, { placeholder });

      const open = hostMethod(host, "openPromptInput");
      if (!open) throw new Error("Prompt host capability is unavailable");
      return open.call(host, {
        placeholder,
        ...promptAnchorCoordinates(),
      });
    },
    { userActivation: true, undefinedRejectionIsCancellation: true },
  ).then((result): PrivateApiResult<string> => {
    if (!result.ok) return result;
    if (result.value == null) return privateApiFailure("cancelled");
    if (typeof result.value.prompt === "string") {
      return { ok: true, value: result.value.prompt };
    }
    disablePrivateCapability("prompt");
    return privateApiFailure("invalid_result");
  });
}

export function requestTargetedReply(text: string): Promise<PrivateApiResult<void>> {
  return invoke(
    "targetedReply",
    (host) => {
      host.requestTargetedReply!({ text });
    },
    { userActivation: true },
  );
}

export function requestFocusedObject(
  title: string,
  params: Record<string, unknown>,
): Promise<PrivateApiResult<void>> {
  return invoke("focusedObject", (host) => {
    host.requestFocusedObject!({ title, params });
  });
}

export function requestCloseFocusedObject(): Promise<PrivateApiResult<void>> {
  return invoke("focusedObject", (host) => {
    host.requestCloseFocusedObject!();
  });
}

export function openConversationOverlay(
  conversationId: string,
  options: { origin?: string; title?: string } = {},
): Promise<PrivateApiResult<void>> {
  if (!conversationId.trim()) return Promise.resolve(privateApiFailure("invalid_result"));
  return invoke("conversationOverlay", (host) => {
    host.openConversationOverlay!({ conversationId, ...options });
  });
}

export function callRawMcp<T = unknown>(
  method: string,
  params?: Record<string, unknown>,
): Promise<PrivateApiResult<T>> {
  if (!RAW_MCP_METHODS.has(method)) {
    return Promise.resolve(privateApiFailure("disabled"));
  }
  return invoke("rawMcp", (host) => host.callMcp!({ method, params }) as Promise<T>);
}

function isCancellation(cause: unknown): boolean {
  if (!(cause instanceof Error)) return false;
  return cause.name === "AbortError" || /cancel|dismiss|closed by user/i.test(cause.message);
}
