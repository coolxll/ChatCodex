// ==UserScript==
// @name         ChatCodex OpenAI 通道覆盖
// @namespace    https://github.com/AeroidesLab/ChatCodex
// @version      1.0.0
// @description  本地通道覆盖，仅改写本浏览器已下载的 JSON：
//               (A) 把自己在连接器列表中的 MCP 连接器从 ONLY_ME/UNTRUSTED/
//               INDIVIDUAL 改写为 OpenAI 第一方通道，宿主不再显示「CSP 已关闭」徽标；
//               (B) 把会话流中的 chatgpt_sdk.distribution_channel 覆盖为 "openai"，
//               用于私有 Host API 测试。纯属本地外观改动：不改动真实的 widget
//               沙箱 CSP、不持久化、也不触碰 OpenAI 服务器。
// @author       ChatCodex
// @match        https://chatgpt.com/*
// @match        https://chat.openai.com/*
// @run-at       document-start
// @noframes
// @grant        unsafeWindow
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        GM_notification
// ==/UserScript==

(() => {
  "use strict";

  const SCRIPT_NAME = "ChatCodex OpenAI 通道覆盖";
  const ENABLED_KEY = "chatcodex_openai_channel_override_enabled";
  const TARGET_CHANNEL = "openai";
  const page = unsafeWindow;
  const enabled = Boolean(GM_getValue(ENABLED_KEY, false));
  const stats = {
    fetchResponses: 0,
    xhrResponses: 0,
    sdkReplacements: 0, // (B) 会话流 chatgpt_sdk 通道覆盖次数
    connectorRewrites: 0, // (A) 连接器列表通道字段改写次数
  };

  // -------------------------------------------------------------------------
  // (A) 连接器列表改写：把开发用 MCP 连接器伪装成第一方，去掉「CSP 已关闭」徽标。
  // -------------------------------------------------------------------------
  const CONNECTOR_SPOOF = {
    status: "ENABLED",
    developer_type: "OAI",
    distribution_channel: "DEFAULT_OAI_CATALOG",
  };

  function isConnectorListUrl(value) {
    try {
      const url = new page.URL(String(value), page.location.href);
      return (
        (url.origin === "https://chatgpt.com" ||
          url.origin === "https://chat.openai.com") &&
        url.pathname.includes("/backend-api/aip/connectors/") &&
        url.pathname.includes("list_accessible")
      );
    } catch {
      return false;
    }
  }

  function shouldRewriteConnector(c) {
    return (
      c &&
      typeof c === "object" &&
      c.connector_type === "MCP" &&
      (c.status === "ONLY_ME" || c.distribution_channel === "INDIVIDUAL")
    );
  }

  function rewriteConnectors(body) {
    let changed = 0;
    const visit = (node) => {
      if (Array.isArray(node)) {
        for (const item of node) visit(item);
        return;
      }
      if (node && typeof node === "object") {
        if (shouldRewriteConnector(node)) {
          Object.assign(node, CONNECTOR_SPOOF);
          changed++;
        }
        for (const value of Object.values(node)) visit(value);
      }
    };
    visit(body);
    return changed;
  }

  function patchConnectorText(text) {
    let body;
    try {
      body = JSON.parse(text);
    } catch {
      return { text, changed: 0 };
    }
    const changed = rewriteConnectors(body);
    return { text: changed > 0 ? JSON.stringify(body) : text, changed };
  }

  // -------------------------------------------------------------------------
  // (B) 会话流覆盖：chatgpt_sdk.distribution_channel -> "openai"（私有 Host API）。
  // -------------------------------------------------------------------------
  function isConversationUrl(value) {
    try {
      const url = new page.URL(String(value), page.location.href);
      if (
        url.origin !== "https://chatgpt.com" &&
        url.origin !== "https://chat.openai.com"
      ) {
        return false;
      }
      return (
        /^\/backend-api\/conversation\/[^/]+\/?$/.test(url.pathname) ||
        url.pathname === "/backend-api/f/conversation" ||
        url.pathname.endsWith("/conversation/resume")
      );
    } catch {
      return false;
    }
  }

  function replaceChannelText(text) {
    if (typeof text !== "string" || text.length === 0) {
      return { text, replacements: 0 };
    }
    let replacements = 0;
    const replace = (match, prefix) => {
      replacements += 1;
      return `${prefix}"${TARGET_CHANNEL}"`;
    };
    const replaceEscaped = (match, prefix) => {
      replacements += 1;
      return `${prefix}\\"${TARGET_CHANNEL}\\"`;
    };
    const patched = text
      .replace(
        /("distribution_channel"\s*:\s*)"(?:only_me|external)"/g,
        replace,
      )
      .replace(
        /(\\"distribution_channel\\"\s*:\s*)\\"(?:only_me|external)\\"/g,
        replaceEscaped,
      );
    return { text: patched, replacements };
  }

  function patchSdkMetadata(value, seen = new WeakSet()) {
    if (!value || typeof value !== "object") {
      return 0;
    }
    if (seen.has(value)) {
      return 0;
    }
    seen.add(value);
    let replacements = 0;
    if (
      value.chatgpt_sdk &&
      typeof value.chatgpt_sdk === "object" &&
      (value.chatgpt_sdk.distribution_channel === "only_me" ||
        value.chatgpt_sdk.distribution_channel === "external")
    ) {
      value.chatgpt_sdk.distribution_channel = TARGET_CHANNEL;
      replacements += 1;
    }
    for (const child of Object.values(value)) {
      if (child && typeof child === "object") {
        replacements += patchSdkMetadata(child, seen);
      }
    }
    return replacements;
  }

  function record(source, url, sdk, connectors) {
    if (sdk <= 0 && connectors <= 0) {
      return;
    }
    stats.sdkReplacements += sdk;
    stats.connectorRewrites += connectors;
    if (source === "fetch") {
      stats.fetchResponses += 1;
    } else {
      stats.xhrResponses += 1;
    }
    const parts = [];
    if (sdk > 0) parts.push(`${sdk} 个 sdk 通道`);
    if (connectors > 0) parts.push(`${connectors} 个连接器`);
    page.console.info(
      `[${SCRIPT_NAME}] 已在 ${source} 中改写 ${parts.join(" + ")}：`,
      url,
    );
  }

  function createPatchedStream(body, url) {
    const decoder = new page.TextDecoder();
    const encoder = new page.TextEncoder();
    let pending = "";
    let sdk = 0;

    return body.pipeThrough(
      new page.TransformStream({
        transform(chunk, controller) {
          pending += decoder.decode(chunk, { stream: true });
          const lineBoundary = pending.lastIndexOf("\n");
          if (lineBoundary < 0) {
            return;
          }
          const result = replaceChannelText(pending.slice(0, lineBoundary + 1));
          sdk += result.replacements;
          controller.enqueue(encoder.encode(result.text));
          pending = pending.slice(lineBoundary + 1);
        },
        flush(controller) {
          pending += decoder.decode();
          const result = replaceChannelText(pending);
          sdk += result.replacements;
          controller.enqueue(encoder.encode(result.text));
          record("fetch", url, sdk, 0);
        },
      }),
    );
  }

  function copyResponseIdentity(target, source) {
    for (const property of ["url", "redirected", "type"]) {
      try {
        Object.defineProperty(target, property, {
          configurable: true,
          enumerable: false,
          value: source[property],
        });
      } catch {
        // 仅诊断用途；以响应体为准。
      }
    }
    return target;
  }

  function patchFetch() {
    if (typeof page.fetch !== "function") {
      return;
    }
    const nativeFetch = page.fetch.bind(page);
    page.fetch = async function chatCodexChannelFetch(input, init) {
      const response = await nativeFetch(input, init);
      const url =
        typeof input === "string" || input instanceof page.URL
          ? String(input)
          : input?.url ?? response.url;

      if (!enabled || !response.body) {
        return response;
      }

      const headers = new page.Headers(response.headers);
      headers.delete("content-length");
      const contentType = headers.get("content-type") ?? "";

      // (A) 连接器列表：解析并改写通道字段。
      if (isConnectorListUrl(url) && contentType.includes("application/json")) {
        const result = patchConnectorText(await response.text());
        record("fetch", url, 0, result.changed);
        return copyResponseIdentity(
          new page.Response(result.text, {
            headers,
            status: response.status,
            statusText: response.statusText,
          }),
          response,
        );
      }

      // (B) 会话流 / 会话 JSON。
      if (isConversationUrl(url)) {
        let body;
        if (contentType.includes("application/json")) {
          const result = replaceChannelText(await response.text());
          record("fetch", url, result.replacements, 0);
          body = result.text;
        } else {
          body = createPatchedStream(response.body, url);
        }
        return copyResponseIdentity(
          new page.Response(body, {
            headers,
            status: response.status,
            statusText: response.statusText,
          }),
          response,
        );
      }

      return response;
    };
  }

  function patchXhr() {
    const Xhr = page.XMLHttpRequest;
    if (typeof Xhr !== "function") {
      return;
    }
    const prototype = Xhr.prototype;
    const nativeOpen = prototype.open;
    const responseDescriptor = Object.getOwnPropertyDescriptor(
      prototype,
      "response",
    );
    const responseTextDescriptor = Object.getOwnPropertyDescriptor(
      prototype,
      "responseText",
    );
    const requestUrl = new WeakMap();
    const logged = new WeakSet();

    prototype.open = function chatCodexChannelOpen(method, url, ...rest) {
      requestUrl.set(this, String(url));
      return nativeOpen.call(this, method, url, ...rest);
    };

    const transform = (xhr, text) => {
      const url = requestUrl.get(xhr);
      if (!enabled || !url) {
        return { text, sdk: 0, connectors: 0 };
      }
      if (isConnectorListUrl(url)) {
        const result = patchConnectorText(text);
        return { text: result.text, sdk: 0, connectors: result.changed };
      }
      if (isConversationUrl(url)) {
        const result = replaceChannelText(text);
        return { text: result.text, sdk: result.replacements, connectors: 0 };
      }
      return { text, sdk: 0, connectors: 0 };
    };

    if (responseTextDescriptor?.get) {
      Object.defineProperty(prototype, "responseText", {
        ...responseTextDescriptor,
        get() {
          const text = responseTextDescriptor.get.call(this);
          const result = transform(this, text);
          if (
            (result.sdk > 0 || result.connectors > 0) &&
            !logged.has(this)
          ) {
            logged.add(this);
            record("xhr", requestUrl.get(this), result.sdk, result.connectors);
          }
          return result.text;
        },
      });
    }

    if (responseDescriptor?.get) {
      Object.defineProperty(prototype, "response", {
        ...responseDescriptor,
        get() {
          const response = responseDescriptor.get.call(this);
          const url = requestUrl.get(this);
          if (!enabled || !url) {
            return response;
          }
          if (this.responseType === "json") {
            let replacements = 0;
            if (isConnectorListUrl(url)) {
              replacements = rewriteConnectors(response);
              if (replacements > 0 && !logged.has(this)) {
                logged.add(this);
                record("xhr", url, 0, replacements);
              }
            } else if (isConversationUrl(url)) {
              replacements = patchSdkMetadata(response);
              if (replacements > 0 && !logged.has(this)) {
                logged.add(this);
                record("xhr", url, replacements, 0);
              }
            }
            return response;
          }
          if (this.responseType === "" || this.responseType === "text") {
            const result = transform(this, response);
            if (
              (result.sdk > 0 || result.connectors > 0) &&
              !logged.has(this)
            ) {
              logged.add(this);
              record("xhr", url, result.sdk, result.connectors);
            }
            return result.text;
          }
          return response;
        },
      });
    }
  }

  function registerControls() {
    GM_registerMenuCommand(
      enabled ? "停用 OpenAI 通道覆盖" : "启用 OpenAI 通道覆盖",
      () => {
        GM_setValue(ENABLED_KEY, !enabled);
        GM_notification({
          title: SCRIPT_NAME,
          text: `已${enabled ? "停用" : "启用"}覆盖，正在刷新 ChatGPT。`,
          timeout: 1800,
        });
        page.setTimeout(() => page.location.reload(), 250);
      },
    );

    GM_registerMenuCommand("查看覆盖状态", () => {
      GM_notification({
        title: SCRIPT_NAME,
        text: enabled
          ? `已启用；本页已改写 ${stats.connectorRewrites} 个连接器、` +
            `${stats.sdkReplacements} 个 sdk 通道。`
          : "已停用。",
        timeout: 3000,
      });
    });
  }

  Object.defineProperty(page, "__chatcodexOpenAIChannelTest", {
    configurable: true,
    value: Object.freeze({
      enabled,
      targetChannel: TARGET_CHANNEL,
      stats,
    }),
  });

  registerControls();
  patchFetch();
  patchXhr();

  page.console.warn(
    `[${SCRIPT_NAME}] 当前${enabled ? "已启用" : "已停用"}。` +
      "仅为本地客户端外观覆盖，不授予服务器权限。" +
      "启用后请强制刷新，让连接器列表重新拉取。",
  );
})();
