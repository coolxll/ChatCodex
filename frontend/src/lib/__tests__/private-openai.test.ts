import assert from "node:assert/strict";
import test from "node:test";
import {
  detectPrivateCapabilities,
  resetPrivateCapabilityFailures,
} from "../private-capabilities.ts";
import {
  callRawMcp,
  requestPrompt,
  showToast,
} from "../private-openai.ts";

function setHost(host: Record<string, unknown> | undefined) {
  Object.assign(globalThis, {
    window: {
      openai: host,
      __CHATCODEX_PRIVATE_API_DISABLED__: [],
    },
  });
  resetPrivateCapabilityFailures();
}

function setUserActivation(active: boolean) {
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { userActivation: { isActive: active } },
  });
}

test("public-only hosts expose no private capability", () => {
  setHost({ callTool() {} });
  assert.deepEqual(detectPrivateCapabilities(), {
    toast: false,
    haptic: false,
    prompt: false,
    targetedReply: false,
    focusedObject: false,
    conversationOverlay: false,
    rawMcp: false,
  });
});

test("the emergency kill switch disables every private capability", () => {
  setHost({
    showToast() {},
    triggerHaptic() {},
    requestPrompt: async () => ({ prompt: "ok" }),
    requestTargetedReply() {},
    requestFocusedObject() {},
    requestCloseFocusedObject() {},
    openConversationOverlay() {},
    callMcp: async () => ({}),
  });
  window.__CHATCODEX_PRIVATE_API_DISABLED__ = true;
  assert.deepEqual(detectPrivateCapabilities(), {
    toast: false,
    haptic: false,
    prompt: false,
    targetedReply: false,
    focusedObject: false,
    conversationOverlay: false,
    rawMcp: false,
  });
  window.__CHATCODEX_PRIVATE_API_DISABLED__ = undefined;
});

test("capabilities are detected independently", () => {
  setHost({
    showToast() {},
    requestFocusedObject() {},
    requestCloseFocusedObject() {},
  });
  const capabilities = detectPrivateCapabilities();
  assert.equal(capabilities.toast, true);
  assert.equal(capabilities.focusedObject, true);
  assert.equal(capabilities.prompt, false);
});

test("openPromptInput is exposed as a compatible prompt capability", () => {
  setHost({
    openPromptInput: async () => ({ prompt: "ok" }),
  });
  assert.equal(detectPrivateCapabilities().prompt, true);
});

test("capability detection does not read missing proxy properties", () => {
  let missingReads = 0;
  setHost(new Proxy(
    {
      openPromptInput: async () => ({ prompt: "ok" }),
    },
    {
      get(target, property, receiver) {
        if (!Reflect.has(target, property)) missingReads += 1;
        return Reflect.get(target, property, receiver);
      },
    },
  ));
  assert.equal(detectPrivateCapabilities().prompt, true);
  assert.equal(missingReads, 0);
});

test("prompt falls back to openPromptInput with an anchor", async () => {
  let received:
    | { placeholder?: string; clientX: number; clientY: number }
    | undefined;
  setUserActivation(true);
  setHost({
    openPromptInput: async (
      input: { placeholder?: string; clientX: number; clientY: number },
    ) => {
      received = input;
      return { prompt: "fallback" };
    },
  });
  const result = await requestPrompt("Name");
  assert.deepEqual(result, { ok: true, value: "fallback" });
  assert.deepEqual(received, {
    placeholder: "Name",
    clientX: 0,
    clientY: 0,
  });
});

test("prompt prefers requestPrompt when both host methods exist", async () => {
  let requestCalls = 0;
  let openCalls = 0;
  setUserActivation(true);
  setHost({
    requestPrompt: async () => {
      requestCalls += 1;
      return { prompt: "preferred" };
    },
    openPromptInput: async () => {
      openCalls += 1;
      return { prompt: "fallback" };
    },
  });
  const result = await requestPrompt("Name");
  assert.deepEqual(result, { ok: true, value: "preferred" });
  assert.equal(requestCalls, 1);
  assert.equal(openCalls, 0);
});

test("a rejected capability is circuit-broken without fallback", async () => {
  let calls = 0;
  setHost({
    showToast() {
      calls += 1;
      throw new Error("host rejected");
    },
  });
  const first = await showToast({ level: "success", title: "Done" });
  const second = await showToast({ level: "success", title: "Done" });
  assert.equal(first.ok ? "" : first.reason, "rejected");
  assert.equal(second.ok ? "" : second.reason, "disabled");
  assert.equal(calls, 1);
});

test("invalid prompt responses disable only prompt", async () => {
  setUserActivation(true);
  setHost({
    requestPrompt: async () => ({ value: "wrong shape" }),
    showToast() {},
  });
  const result = await requestPrompt("Name");
  assert.equal(result.ok ? "" : result.reason, "invalid_result");
  assert.equal(detectPrivateCapabilities().prompt, false);
  assert.equal(detectPrivateCapabilities().toast, true);
});

test("user-activation methods are not invoked outside a user gesture", async () => {
  let calls = 0;
  setHost({
    requestTargetedReply() {
      calls += 1;
    },
  });
  setUserActivation(false);
  const { requestTargetedReply } = await import("../private-openai.ts");
  const result = await requestTargetedReply("target");
  assert.equal(result.ok ? "" : result.reason, "user_activation_required");
  assert.equal(calls, 0);
  assert.equal(detectPrivateCapabilities().targetedReply, true);
});

test("cancelled prompts do not circuit-break prompt", async () => {
  setHost({ requestPrompt: async () => null });
  setUserActivation(true);
  const result = await requestPrompt("Name");
  assert.equal(result.ok ? "" : result.reason, "cancelled");
  assert.equal(detectPrivateCapabilities().prompt, true);
});

test("prompt rejection without a reason is treated as host cancellation", async () => {
  setHost({ requestPrompt: async () => Promise.reject() });
  setUserActivation(true);
  const result = await requestPrompt("Name");
  assert.equal(result.ok ? "" : result.reason, "cancelled");
  assert.equal(detectPrivateCapabilities().prompt, true);
});

test("raw MCP is read-only allowlisted", async () => {
  let calls = 0;
  setHost({
    callMcp: async () => {
      calls += 1;
      return { ok: true };
    },
  });
  const rejected = await callRawMcp("tools/call", {});
  assert.equal(rejected.ok ? "" : rejected.reason, "disabled");
  assert.equal((await callRawMcp("resources/list", {})).ok, true);
  assert.equal(calls, 1);
});
