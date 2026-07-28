/** 面板与后端 /api 的客户端。 */
const BASE = "";

function headers(token = "") {
  return { ...(token ? { authorization: `Bearer ${token}` } : {}), "content-type": "application/json" };
}

async function req(token: string, path: string, opts: RequestInit = {}) {
  const r = await fetch(BASE + path, { credentials: "same-origin", ...opts, headers: { ...headers(token), ...(opts.headers || {}) } });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.status === 204 ? null : r.json();
}

export const api = {
  health: () => fetch(BASE + "/healthz").then((r) => r.json()),
  login: (token: string) => req("", "/api/auth/session", { method: "POST", body: JSON.stringify({ token }) }),
  authStatus: () => req("", "/api/auth/session"),
  logout: () => req("", "/api/auth/session", { method: "DELETE" }),
  overview: (token: string) => req(token, "/api/overview"),
  // WebChat 执行上下文
  executionContexts: (token: string, status = "") =>
    req(token, `/api/execution-contexts${status ? `?status=${status}` : ""}`),
  setExecutionContextStatus: (token: string, contextId: string, status: string) =>
    req(token, `/api/execution-contexts/${contextId}/status`, {
      method: "POST",
      body: JSON.stringify({ status }),
    }),
  // 设置
  settings: (token: string) => req(token, "/api/settings"),
  oauthMetadataAudit: (token: string) => req(token, "/api/oauth/metadata-audit"),
  mcpTools: (token: string) => req(token, "/api/mcp-tools"),
  setMcpToolPolicy: (token: string, policies: Record<string, string>) =>
    req(token, "/api/mcp-tools/policy", { method: "POST", body: JSON.stringify({ policies }) }),
  setSettings: (token: string, kv: Record<string, any>) =>
    req(token, "/api/settings", { method: "POST", body: JSON.stringify(kv) }),
  // app-server
  appserverStatus: (token: string) => req(token, "/api/appserver/status"),
  appserverRestart: (token: string) => req(token, "/api/appserver/restart", { method: "POST" }),
  appserverStop: (token: string) => req(token, "/api/appserver/stop", { method: "POST" }),
  nativeStatus: (token: string) => req(token, "/api/native/status"),
  installCodex: (token: string, body: any = {}) =>
    req(token, "/api/native/codex/install", { method: "POST", body: JSON.stringify(body) }),
  installTunnelClient: (token: string, release = "") =>
    req(token, "/api/native/tunnel-client/install", { method: "POST", body: JSON.stringify({ release }) }),
  // 全局公网入口：仅 direct / cloudflared
  publicRouteStatus: (token: string) => req(token, "/api/public-route/status"),
  publicRouteStart: (token: string, body: any) =>
    req(token, "/api/public-route/start", { method: "POST", body: JSON.stringify(body) }),
  publicRouteStop: (token: string) =>
    req(token, "/api/public-route/stop", { method: "POST" }),
  // ChatGPT Tunnel：独立 MCP 传输
  chatgptTunnelStatus: (token: string) => req(token, "/api/chatgpt-tunnel/status"),
  chatgptTunnelStart: (token: string, body: any) =>
    req(token, "/api/chatgpt-tunnel/start", { method: "POST", body: JSON.stringify(body) }),
  chatgptTunnelStop: (token: string) =>
    req(token, "/api/chatgpt-tunnel/stop", { method: "POST" }),
  // 审批
  approvals: (token: string, conversationId = "") =>
    req(token, `/api/approvals${conversationId ? `?conversationId=${encodeURIComponent(conversationId)}` : ""}`),
  resolveApproval: (
    token: string,
    requestId: string,
    decision: string,
    expectedVersion?: number,
    conversationId?: string,
  ) =>
    req(token, `/api/approvals/${encodeURIComponent(requestId)}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision, expectedVersion, conversationId }),
    }),
  eventStreamUrl: (conversationId: string, lastEventId = 0) =>
    `/api/events?conversationId=${encodeURIComponent(conversationId)}&lastEventId=${lastEventId}`,
};
