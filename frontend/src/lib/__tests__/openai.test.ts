import assert from "node:assert/strict";
import test from "node:test";

test("UI-only state is not rewritten as model context and ui/message uses the standard schema", async () => {
  const messages: Array<Record<string, any>> = [];
  const hostOrigin = "https://host.example";
  const fakeWindow = new EventTarget() as EventTarget & Record<string, any>;
  const fakeParent = {
    postMessage(message: Record<string, any>) {
      messages.push(message);
      if (message.id == null) return;
      const result = message.method === "ui/initialize"
        ? { hostContext: { displayMode: "inline" } }
        : {};
      queueMicrotask(() => {
        const response = new Event("message") as Event & Record<string, any>;
        Object.defineProperties(response, {
          source: { value: fakeParent },
          origin: { value: hostOrigin },
          data: {
            value: { jsonrpc: "2.0", id: message.id, result },
          },
        });
        fakeWindow.dispatchEvent(response);
      });
    },
  };
  Object.assign(fakeWindow, {
    parent: fakeParent,
    openai: undefined,
    setTimeout: globalThis.setTimeout.bind(globalThis),
    clearTimeout: globalThis.clearTimeout.bind(globalThis),
    dispatchEvent: fakeWindow.dispatchEvent.bind(fakeWindow),
    addEventListener: fakeWindow.addEventListener.bind(fakeWindow),
    removeEventListener: fakeWindow.removeEventListener.bind(fakeWindow),
  });
  Object.assign(globalThis, {
    window: fakeWindow,
    document: {
      referrer: `${hostOrigin}/conversation`,
      body: { scrollHeight: 0, scrollWidth: 0 },
    },
  });

  const OA = await import("../openai.ts");
  const off = OA.onOpenAiEvent(() => {});

  OA.setWidgetState({ selected: "src/app.ts" });
  assert.equal(messages.length, 0);
  assert.deepEqual(OA.widgetState(), { selected: "src/app.ts" });

  await OA.sendFollowUpMessage("继续运行测试");
  assert.equal(messages[0]?.method, "ui/initialize");
  assert.equal(messages[1]?.method, "ui/notifications/initialized");
  assert.equal(messages[2]?.method, "ui/message");
  assert.deepEqual(messages[2]?.params, {
    role: "user",
    content: [{ type: "text", text: "继续运行测试" }],
  });
  assert.equal(
    messages.some((message) => message.method === "ui/update-model-context"),
    false,
  );
  off();
});
