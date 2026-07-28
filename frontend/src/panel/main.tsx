import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Archive, Bot, Check, ChevronRight, CircleHelp, Cloud, Code2,
  Copy, Download, ExternalLink, Eye, EyeOff, FileClock, Gauge, KeyRound, Layers3,
  LockKeyhole, LogOut, Menu, Moon, Network, Play, RefreshCw, Save,
  Server, Settings2, ShieldCheck, Square, Sun, TerminalSquare, Users,
  X, Zap,
} from "lucide-react";
import "../styles.css";
import { api } from "./api";
import { cn } from "../lib/utils";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Badge } from "../components/ui/badge";
import { Separator } from "../components/ui/separator";
import { Switch } from "../components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../components/ui/select";

type Tab = "overview" | "contexts" | "tunnel" | "settings" | "approvals" | "codex";
const NAV: Array<{ id: Tab; label: string; icon: React.ElementType }> = [
  { id: "overview", label: "概览", icon: Gauge },
  { id: "contexts", label: "执行上下文", icon: Layers3 },
  { id: "tunnel", label: "公网入口", icon: Network },
  { id: "approvals", label: "审批", icon: ShieldCheck },
  { id: "codex", label: "Codex", icon: TerminalSquare },
  { id: "settings", label: "设置", icon: Settings2 },
];

function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [overview, setOverview] = useState<any>(null);
  const [navOpen, setNavOpen] = useState(false);
  const [dark, setDark] = useState(localStorage.getItem("cc_theme") === "dark");

  useEffect(() => {
    // Remove the pre-split UI's JavaScript-readable bearer token on upgrade.
    localStorage.removeItem("cc_token");
    sessionStorage.removeItem("cc_token");
  }, []);
  useEffect(() => { api.authStatus().then(() => setAuthenticated(true)).catch(() => setAuthenticated(false)); }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.documentElement.style.colorScheme = dark ? "dark" : "light";
    localStorage.setItem("cc_theme", dark ? "dark" : "light");
  }, [dark]);
  useEffect(() => {
    if (!authenticated) return;
    const load = () => api.overview("").then(setOverview).catch((e) => {
      if (String(e).startsWith("Error: 401")) setAuthenticated(false);
    });
    load(); const timer = window.setInterval(load, 5000); return () => clearInterval(timer);
  }, [authenticated]);

  if (authenticated === null) return <Splash />;
  if (!authenticated) return <Login onSuccess={() => setAuthenticated(true)} dark={dark} setDark={setDark} />;

  const selectTab = (value: Tab) => { setTab(value); setNavOpen(false); };
  return (
    <div className="min-h-screen bg-muted/35 text-foreground">
      <aside className={cn("fixed inset-y-0 left-0 z-40 flex w-64 flex-col border-r bg-background transition-transform lg:translate-x-0", navOpen ? "translate-x-0" : "-translate-x-full")}>
        <Brand />
        <nav className="flex-1 space-y-1 px-3 py-4">
          <p className="mb-2 px-3 text-[11px] font-semibold uppercase tracking-[.14em] text-muted-foreground">Workspace</p>
          {NAV.map(({ id, label, icon: Icon }) => (
            <button key={id} onClick={() => selectTab(id)} className={cn("flex h-10 w-full items-center gap-3 rounded-lg px-3 text-sm font-medium transition-colors", tab === id ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent hover:text-foreground")}>
              <Icon className="h-4 w-4" />{label}
              {id === "approvals" && overview?.pendingApprovals > 0 && <Badge variant="warning" className="ml-auto px-1.5">{overview.pendingApprovals}</Badge>}
            </button>
          ))}
        </nav>
        <div className="m-3 rounded-xl border bg-muted/40 p-3">
          <StatusLine label="Codex" on={overview?.appserver?.running} />
          <StatusLine label="公网入口" on={overview?.publicRoute?.running} />
          <StatusLine label="MCP Tunnel" on={overview?.chatgptTunnel?.running} />
        </div>
        <div className="flex items-center gap-1 border-t p-3">
          <Button variant="ghost" size="icon" aria-label="切换主题" onClick={() => setDark(!dark)}>{dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}</Button>
          <Button variant="ghost" className="ml-auto text-muted-foreground" onClick={async () => { await api.logout(); setAuthenticated(false); }}><LogOut className="h-4 w-4" />退出</Button>
        </div>
      </aside>
      {navOpen && <button aria-label="关闭导航" className="fixed inset-0 z-30 bg-black/30 lg:hidden" onClick={() => setNavOpen(false)} />}

      <div className="lg:pl-64">
        <header className="sticky top-0 z-20 flex h-16 items-center gap-3 border-b bg-background/90 px-4 backdrop-blur lg:px-8">
          <Button variant="ghost" size="icon" className="lg:hidden" aria-label="打开导航" onClick={() => setNavOpen(true)}><Menu className="h-5 w-5" /></Button>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold">{NAV.find((item) => item.id === tab)?.label}</p>
            <p className="truncate text-xs text-muted-foreground">ChatCodex Gateway 控制台</p>
          </div>
          <Badge variant={overview?.appserver?.healthy ? "success" : "secondary"} className="ml-auto gap-1.5"><span className={cn("h-1.5 w-1.5 rounded-full", overview?.appserver?.healthy ? "bg-emerald-500" : "bg-muted-foreground")} />{overview?.appserver?.healthy ? "系统正常" : "等待服务"}</Badge>
        </header>
        <main className="mx-auto max-w-7xl p-4 sm:p-6 lg:p-8">
          {tab === "overview" && <Overview data={overview} go={selectTab} />}
          {tab === "contexts" && <ExecutionContexts />}
          {tab === "tunnel" && <Tunnel />}
          {tab === "settings" && <Settings />}
          {tab === "approvals" && <Approvals />}
          {tab === "codex" && <Codex />}
        </main>
      </div>
    </div>
  );
}

function Login({ onSuccess, dark, setDark }: { onSuccess(): void; dark: boolean; setDark(v: boolean): void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setError("");
    try { await api.login(token.trim()); setToken(""); onSuccess(); }
    catch { setError("Web Access Token 不正确，请检查 Gateway 启动输出或环境配置。"); }
    finally { setBusy(false); }
  }
  return (
    <div className="grid min-h-screen bg-muted/30 lg:grid-cols-[1.1fr_.9fr]">
      <div className="relative hidden overflow-hidden border-r bg-zinc-950 p-12 text-white lg:flex lg:flex-col">
        <div className="absolute inset-0 opacity-40 [background-image:radial-gradient(circle_at_20%_20%,#10a37f_0,transparent_35%),radial-gradient(circle_at_80%_70%,#2563eb_0,transparent_30%)]" />
        <div className="relative"><Brand inverse /></div>
        <div className="relative my-auto max-w-lg">
          <Badge className="mb-5 border-white/15 bg-white/10 text-white">本地优先的智能体网关</Badge>
          <h1 className="text-5xl font-semibold leading-[1.08] tracking-tight">在 ChatGPT 中安全运行本地 Codex。</h1>
          <p className="mt-5 max-w-md text-base leading-7 text-zinc-300">在此管理工作区、MCP 工具、审批与安全隧道。</p>
        </div>
        <p className="relative text-xs text-zinc-500">Web Access Token 仅用于登录本控制台。</p>
      </div>
      <div className="relative flex items-center justify-center p-6">
        <Button variant="ghost" size="icon" className="absolute right-5 top-5" aria-label="切换主题" onClick={() => setDark(!dark)}>{dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}</Button>
        <form onSubmit={submit} className="w-full max-w-sm">
          <div className="mb-8 lg:hidden"><Brand /></div>
          <div className="mb-7">
            <div className="mb-4 flex h-11 w-11 items-center justify-center rounded-xl bg-primary/10 text-primary"><LockKeyhole className="h-5 w-5" /></div>
            <h2 className="text-2xl font-semibold tracking-tight">连接 Gateway</h2>
            <p className="mt-2 text-sm leading-6 text-muted-foreground">输入 Web Access Token 以登录控制台。Token 见 Gateway 启动输出。</p>
          </div>
          <div className="space-y-2"><Label htmlFor="web-token">Web Access Token</Label><Input id="web-token" type="password" autoFocus autoComplete="current-password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="粘贴 Web Access Token" className="h-11 font-mono" /></div>
          {error && <div role="alert" className="mt-3 rounded-lg border border-destructive/20 bg-destructive/5 p-3 text-sm text-destructive">{error}</div>}
          <Button className="mt-5 h-11 w-full" disabled={!token.trim() || busy}>{busy ? <RefreshCw className="h-4 w-4 animate-spin" /> : <KeyRound className="h-4 w-4" />}{busy ? "正在验证" : "进入控制台"}</Button>
        </form>
      </div>
    </div>
  );
}

function Overview({ data, go }: { data: any; go(tab: Tab): void }) {
  if (!data) return <Loading text="正在读取 Gateway 状态" />;
  const stats = [
    { label: "活跃上下文", value: data.executionContexts?.active ?? 0, detail: `共 ${data.executionContexts?.total ?? 0} 个工作区绑定`, icon: Users, tab: "contexts" as Tab },
    { label: "待处理审批", value: data.pendingApprovals ?? 0, detail: data.pendingApprovals ? "需要你的决策" : "队列为空", icon: ShieldCheck, tab: "approvals" as Tab },
    { label: "Codex Runtime", value: data.appserver?.running ? "在线" : "离线", detail: `重启 ${data.appserver?.restartCount ?? 0} 次`, icon: Bot, tab: "codex" as Tab },
    { label: "公网入口", value: data.publicRoute?.running ? "已启用" : "未启用", detail: data.publicRoute?.kind ?? "none", icon: Cloud, tab: "tunnel" as Tab },
  ];
  const publicEndpoint = data.publicRoute?.url?.startsWith("http") ? mcpUrl(data.publicRoute.url) : data.publicRoute?.url;
  const secureTunnel = data.chatgptTunnel?.tunnelId || data.chatgptTunnel?.url;
  return (
    <Page title="运行概览" description="WebChat、Gateway 独立执行和审批的实时状态。">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">{stats.map(({ icon: Icon, ...item }) => <Card key={item.label} className="group cursor-pointer transition hover:-translate-y-0.5 hover:border-primary/30 hover:shadow-md" onClick={() => go(item.tab)}><CardContent className="p-5"><div className="flex items-start justify-between"><div><p className="text-sm text-muted-foreground">{item.label}</p><p className="mt-2 text-2xl font-semibold tracking-tight">{item.value}</p></div><div className="rounded-lg bg-primary/10 p-2.5 text-primary"><Icon className="h-4 w-4" /></div></div><p className="mt-3 text-xs text-muted-foreground">{item.detail}</p></CardContent></Card>)}</div>
      <div className="mt-6 grid gap-6 xl:grid-cols-[1.3fr_.7fr]">
        <Card><CardHeader><CardTitle>ChatGPT MCP 入口</CardTitle><CardDescription>对外提供的 MCP 接入地址。</CardDescription></CardHeader><CardContent className="space-y-3">{publicEndpoint && <CopyValue value={publicEndpoint} />}{secureTunnel && <CopyValue value={secureTunnel} />}{!publicEndpoint && !secureTunnel && <Empty icon={Network} title="尚未建立 MCP 入口" detail="配置 Cloudflare/直接暴露，或在设置中启用 ChatGPT Tunnel。" action={<Button size="sm" variant="outline" onClick={() => go("tunnel")}>配置公网入口<ChevronRight className="h-3.5 w-3.5" /></Button>} />}</CardContent></Card>
        <Card><CardHeader><CardTitle>认证</CardTitle><CardDescription>控制台与 MCP 分别使用独立凭据。</CardDescription></CardHeader><CardContent className="space-y-3"><Boundary icon={LockKeyhole} title="Web" value="Access Token" detail="控制台登录" /><Boundary icon={KeyRound} title="MCP" value={data.auth?.mcp ?? "—"} detail="MCP 工具调用" /></CardContent></Card>
      </div>
    </Page>
  );
}

function ExecutionContexts() {
  const [items, setItems] = useState<any[]>([]); const [selected, setSelected] = useState<any>(null); const [error, setError] = useState("");
  const load = () => api.executionContexts("").then((d) => setItems(d.contexts ?? [])).catch((e) => setError(String(e)));
  useEffect(() => { load(); }, []);
  async function status(item: any, value: string) { await api.setExecutionContextStatus("", item.id, value); setSelected(null); load(); }
  return <Page title="执行上下文" description="每个上下文把 WebChat 对话绑定到本地工作区、沙箱与审批策略。" actions={<Button variant="outline" size="sm" onClick={load}><RefreshCw className="h-3.5 w-3.5" />刷新</Button>}>
    {error && <Alert>{error}</Alert>}
    {!items.length ? <Card><CardContent className="p-6"><Empty icon={FileClock} title="暂无执行上下文" detail="在 WebChat 工作区面板保存目录和安全设置后会显示在这里。" /></CardContent></Card> : <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
      <Card className="overflow-hidden"><div className="divide-y">{items.map((item) => <button key={item.id} onClick={() => setSelected(item)} className={cn("flex w-full items-center gap-4 p-4 text-left transition hover:bg-accent/60", selected?.id === item.id && "bg-accent")}><div className="rounded-lg bg-muted p-2"><Code2 className="h-4 w-4" /></div><div className="min-w-0 flex-1"><div className="flex items-center gap-2"><span className="truncate text-sm font-medium">{item.cwd || "未命名工作区"}</span><Badge variant={item.status === "active" ? "success" : "secondary"}>{item.status}</Badge></div><p className="mt-1 truncate font-mono text-xs text-muted-foreground">{item.conversationId}</p></div><ChevronRight className="h-4 w-4 text-muted-foreground" /></button>)}</div></Card>
      <Card className="h-fit xl:sticky xl:top-24">{selected ? <><CardHeader className="flex-row items-start justify-between"><div><CardTitle>上下文详情</CardTitle><CardDescription className="mt-1">{selected.workMode} · {selected.sandboxMode}</CardDescription></div><Button size="icon" variant="ghost" onClick={() => setSelected(null)}><X className="h-4 w-4" /></Button></CardHeader><CardContent className="space-y-4"><Details data={[["Conversation", selected.conversationId], ["目录", selected.cwd], ["工作区根", selected.workspaceRoots?.join(", ")], ["审批", typeof selected.approvalPolicy === "string" ? selected.approvalPolicy : JSON.stringify(selected.approvalPolicy)], ["Permission profile", selected.permissionProfileId || "未设置"], ["版本", selected.version], ["Codex agent", "禁用"]]} /><div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" onClick={() => status(selected, selected.status === "active" ? "archived" : "active")}><Archive className="h-3.5 w-3.5" />{selected.status === "active" ? "归档" : "恢复"}</Button></div></CardContent></> : <CardContent className="p-6"><Empty icon={CircleHelp} title="选择一个执行上下文" detail="查看工作区、安全策略和版本。" /></CardContent>}</Card>
    </div>}
  </Page>;
}

function Tunnel() {
  const [state, setState] = useState<any>(null);
  const [kind, setKind] = useState("direct");
  const [mode, setMode] = useState("try");
  const [tunnelToken, setTunnelToken] = useState("");
  const [mcpMode, setMcpMode] = useState("");
  const [publicUrl, setPublicUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = () => api.publicRouteStatus("").then(setState).catch(() => {});
  useEffect(() => {
    load();
    api.settings("").then((d) => {
      setMcpMode(d.settings?.mcp_auth_mode ?? "");
      setPublicUrl(d.settings?.public_url ?? "");
      const route = d.settings?.public_route_kind ?? "";
      if (route.startsWith("cloudflared-")) {
        setKind("cloudflared"); setMode(route.slice("cloudflared-".length));
      } else if (route === "direct") setKind("direct");
    });
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, []);
  async function start() {
    setBusy(true); setError("");
    try {
      const body = kind === "cloudflared"
        ? { kind, mode, token: tunnelToken }
        : { kind: "direct" };
      const result = await api.publicRouteStart("", body);
      setState(result);
      if (!result.running) setError(result.detail || "公网入口未就绪");
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  const endpoint = state?.url?.startsWith("http") ? mcpUrl(state.url) : state?.url;
  return <Page title="全局公网入口" description="选择 Cloudflare 或直接暴露，对外提供 Gateway 服务。">
    <div className="grid gap-6 xl:grid-cols-[.8fr_1.2fr]">
      <div className="space-y-4">
        <Card><CardHeader><CardTitle>入口状态</CardTitle><CardDescription>对外提供 Web、API 与可选直连 MCP 的公网路径。</CardDescription></CardHeader><CardContent className="space-y-4"><div className="flex items-center gap-3"><div className={cn("rounded-full p-3", state?.running ? "bg-emerald-500/10 text-emerald-600" : "bg-muted text-muted-foreground")}><Zap className="h-5 w-5" /></div><div><p className="font-medium">{state?.running ? "公网入口已启用" : "公网入口未启用"}</p><p className="text-sm text-muted-foreground">{state?.kind ?? "none"}</p></div></div>{endpoint && <CopyValue value={endpoint} />}{state?.detail && <p className="rounded-lg bg-muted p-3 text-xs leading-5 text-muted-foreground">{state.detail}</p>}{state?.lastError && <Alert>{state.lastError}</Alert>}</CardContent></Card>
        <Card><CardHeader><CardTitle>当前运行地址</CardTitle><CardDescription>对外服务使用的公网地址。</CardDescription></CardHeader><CardContent><CopyValue value={state?.url || publicUrl || "未配置"} /></CardContent></Card>
      </div>
      <Card><CardHeader><CardTitle>选择公网方式</CardTitle><CardDescription>在 Cloudflare 与直接暴露之间二选一。</CardDescription></CardHeader><CardContent className="space-y-5"><div className="grid gap-2 sm:grid-cols-2">{[["cloudflared", "Cloudflare", Cloud], ["direct", "直接暴露", ExternalLink]].map(([value, label, Icon]: any) => <button key={value} onClick={() => setKind(value)} className={cn("rounded-xl border p-3 text-left transition", kind === value ? "border-primary bg-primary/5 ring-1 ring-primary" : "hover:bg-accent")}><Icon className="mb-3 h-4 w-4" /><p className="text-sm font-medium">{label}</p></button>)}</div>{kind === "cloudflared" ? <><Field label="Cloudflare 模式"><Select value={mode} onValueChange={setMode}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="try">临时域名（仅调试）</SelectItem><SelectItem value="named">固定域名（生产）</SelectItem></SelectContent></Select></Field>{mode === "named" && <Field label="Tunnel Token"><SecretInput value={tunnelToken} setValue={setTunnelToken} placeholder="Cloudflare tunnel JWT" /></Field>}{mode === "try" && ["oauth", "both"].includes(mcpMode) && <Alert tone="warning">临时域名每次重启都会变化，已有 OAuth 客户端需重新连接；生产请用固定域名。</Alert>}</> : <Alert>直接暴露不启动代理；请确保公网 URL、TLS 证书、DNS/NAT 已指向本 Gateway。</Alert>}{!["oauth", "both"].includes(mcpMode) && <Alert tone="warning">公网 MCP 建议使用 OAuth。</Alert>}{error && <Alert>{error}</Alert>}<div className="flex gap-2"><Button onClick={start} disabled={busy}>{busy ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}启用</Button><Button variant="outline" onClick={() => api.publicRouteStop("").then(load)}><Square className="h-3.5 w-3.5" />停止</Button></div></CardContent></Card>
    </div>
  </Page>;
}

function ChatGptMcpTunnel({ cfg, set }: { cfg: Record<string, any>; set(key: string, value: any): void }) {
  const [state, setState] = useState<any>(null);
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");
  const load = () => api.chatgptTunnelStatus("").then(setState).catch(() => {});
  useEffect(() => { load(); const timer = setInterval(load, 3000); return () => clearInterval(timer); }, []);
  const oauthMode = ["oauth", "both"].includes(cfg.mcp_auth_mode);
  const issuer = String(cfg.runtime_public_url || cfg.public_url || "");
  const issuerReady = /^https:\/\/(?!localhost(?::|\/)|127\.0\.0\.1(?::|\/)|\[?::1\]?)/i.test(issuer);
  const oauthBlocked = oauthMode && !issuerReady;
  async function start() {
    setBusy(true); setError("");
    try {
      const result = await api.chatgptTunnelStart("", {
        tunnel_id: cfg.chatgpt_tunnel_id ?? "", api_key: apiKey,
        client_bin: cfg.tunnel_client_command ?? "",
      });
      setState(result); set("chatgpt_tunnel_enabled", true);
      if (!result.running || !result.ready) setError(result.detail || "ChatGPT Tunnel 尚未就绪");
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  async function stop() {
    setBusy(true); setError("");
    try { await api.chatgptTunnelStop(""); set("chatgpt_tunnel_enabled", false); await load(); }
    catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  async function download() {
    setDownloading(true); setError("");
    try { await api.installTunnelClient("", cfg.tunnel_client_release || "v0.0.11-dev"); await load(); }
    catch (e) { setError(String(e)); } finally { setDownloading(false); }
  }
  return <Card><CardHeader><CardTitle className="flex items-center gap-2"><ShieldCheck className="h-4 w-4 text-primary" />ChatGPT Tunnel · MCP</CardTitle><CardDescription>仅把 `/mcp/` 提供给 ChatGPT，不作为全局公网入口。</CardDescription></CardHeader><CardContent className="space-y-5"><div className="grid gap-4 md:grid-cols-2"><Field label="Tunnel ID" hint="OpenAI 控制平面的 tunnel_… 标识。"><Input value={cfg.chatgpt_tunnel_id ?? ""} onChange={(e) => set("chatgpt_tunnel_id", e.target.value)} placeholder="tunnel_…" /></Field><Field label="Runtime API Key" hint="仅保留在当前进程；自动启动请用 CONTROL_PLANE_API_KEY。"><SecretInput value={apiKey} setValue={setApiKey} placeholder="sk-…" /></Field><Field label="tunnel-client 版本"><Input value={cfg.tunnel_client_release ?? "v0.0.11-dev"} onChange={(e) => set("tunnel_client_release", e.target.value)} className="font-mono text-xs" /></Field><div className="flex items-end"><Button variant="outline" onClick={download} disabled={downloading}>{downloading ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}下载 / 更新客户端</Button></div></div><div className="grid gap-3 sm:grid-cols-2"><div className="flex items-center justify-between rounded-lg border p-3"><div><Label>随 Gateway 自动启动</Label><p className="mt-1 text-xs text-muted-foreground">需要环境变量中的 Runtime API Key</p></div><Switch checked={cfg.chatgpt_tunnel_enabled ?? false} onCheckedChange={(v) => set("chatgpt_tunnel_enabled", v)} /></div><div className="flex items-center justify-between rounded-lg border p-3"><div><Label>异常自动重启</Label><p className="mt-1 text-xs text-muted-foreground">独立线程与有界退避</p></div><Switch checked={cfg.tunnel_auto_restart ?? true} onCheckedChange={(v) => set("tunnel_auto_restart", v)} /></div></div>{oauthMode ? (oauthBlocked ? <Alert tone="warning">OAuth 需要先把全局公网 URL 配成可公开访问的 HTTPS 地址。</Alert> : <Alert>OAuth 授权服务器地址 {issuer} 必须能被 ChatGPT 公开访问。</Alert>) : <Alert>Token 模式下 MCP Access Token 只用于本机私有连接，不提供给 ChatGPT。</Alert>}{error && <Alert>{error}</Alert>}<div className="rounded-lg border p-3"><div className="flex flex-wrap items-center gap-3"><Badge variant={state?.ready ? "success" : state?.running ? "warning" : "secondary"}>{state?.ready ? "已就绪" : state?.running ? "启动中" : "已停止"}</Badge><span className="text-xs text-muted-foreground">PID {state?.pid || "—"} · {state?.detail || "未启动"}</span><div className="ml-auto flex gap-2"><Button size="sm" onClick={start} disabled={busy || oauthBlocked}>{busy ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}启动</Button><Button size="sm" variant="outline" onClick={stop} disabled={busy}><Square className="h-3.5 w-3.5" />停止</Button></div></div>{state?.kind === "chatgpt" && <div className="mt-3 grid grid-cols-3 gap-2"><MiniStatus label="进程" on={state.running} /><MiniStatus label="健康" on={state.healthy} /><MiniStatus label="就绪" on={state.ready} /></div>}{state?.logs?.length > 0 && <details className="mt-3 text-xs"><summary className="cursor-pointer font-medium">最近日志</summary><pre className="mt-3 max-h-48 overflow-auto whitespace-pre-wrap text-muted-foreground">{state.logs.join("\n")}</pre></details>}</div></CardContent></Card>;
}

function Settings() {
  const [cfg, setCfg] = useState<Record<string, any>>({}); const [audit, setAudit] = useState<any>(null); const [saved, setSaved] = useState(false); const [saving, setSaving] = useState(false); const [error, setError] = useState("");
  useEffect(() => { api.settings("").then((d) => setCfg(d.settings ?? {})); api.oauthMetadataAudit("").then(setAudit).catch(() => {}); }, []);
  const set = (key: string, value: any) => { setCfg((now) => ({ ...now, [key]: value })); setSaved(false); };
  async function save() { setSaving(true); setError(""); const payload = { ...cfg }; for (const key of ["web_access_token", "mcp_access_token", "oauth_password", "codex_external_ws_key", "codex_internal_ws_key"]) if (!payload[key] || payload[key] === "********") delete payload[key]; try { const result = await api.setSettings("", payload); setCfg(result.settings ?? cfg); setSaved(true); api.oauthMetadataAudit("").then(setAudit).catch(() => {}); } catch (e) { setError(String(e)); } finally { setSaving(false); } }
  return <Page title="Gateway 设置" description="认证、安全策略、Codex Runtime 与模型工具曝光范围。" actions={<Button onClick={save} disabled={saving}>{saving ? <RefreshCw className="h-4 w-4 animate-spin" /> : saved ? <Check className="h-4 w-4" /> : <Save className="h-4 w-4" />}{saved ? "已保存" : "保存更改"}</Button>}>
    {error && <Alert>{error}</Alert>}
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
      <div className="space-y-6">
        <Card><CardHeader><CardTitle className="flex items-center gap-2"><LockKeyhole className="h-4 w-4 text-primary" />访问与认证</CardTitle><CardDescription>Web 与 MCP 凭据相互独立，更改后需重启 Gateway。</CardDescription></CardHeader><CardContent className="space-y-6"><SecurityBlock number="01" title="Web Access Token" description="仅用于登录本控制台。"><SecretSetting value={cfg.web_access_token} onChange={(v) => set("web_access_token", v)} placeholder="输入新的 Web Access Token" /></SecurityBlock><Separator /><SecurityBlock number="02" title="MCP 认证" description="保护 /mcp 工具调用。Token 适合本机或 Tunnel；OAuth 需可公开访问的 HTTPS 地址。"><div className="space-y-4"><Field label="认证模式"><Select value={cfg.mcp_auth_mode ?? "token"} onValueChange={(v) => set("mcp_auth_mode", v)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="token">仅 Token（ChatGPT Tunnel 推荐）</SelectItem><SelectItem value="both">Token + OAuth（需公网 issuer）</SelectItem><SelectItem value="oauth">仅 OAuth（需公网 issuer）</SelectItem><SelectItem value="noauth">无认证（仅本机）</SelectItem></SelectContent></Select></Field>{["token", "both"].includes(cfg.mcp_auth_mode ?? "token") && <Field label="MCP Access Token" hint="仅用于 /mcp；不要填入 Web 登录框。"><SecretSetting value={cfg.mcp_access_token} onChange={(v) => set("mcp_access_token", v)} placeholder="输入新的 MCP Access Token" /></Field>}{["oauth", "both"].includes(cfg.mcp_auth_mode) && <Field label="OAuth 授权密码" hint="连接时在授权页输入。"><SecretSetting value={cfg.oauth_password} onChange={(v) => set("oauth_password", v)} placeholder="输入新的 OAuth 授权密码" /></Field>}<Field label="固定公网 URL" hint="OAuth 需要这里的固定 HTTPS 根地址。"><Input value={cfg.public_url ?? ""} onChange={(e) => set("public_url", e.target.value)} placeholder="https://codex.example.com" /></Field><div className="flex items-center justify-between rounded-lg border p-3"><div><Label>OAuth 回调保护（高级）</Label><p className="mt-1 text-xs text-muted-foreground">仅允许 https://chatgpt.com/connector/oauth/*。</p></div><Switch checked={cfg.oauth_callback_protection ?? false} onCheckedChange={(v) => set("oauth_callback_protection", v)} /></div></div></SecurityBlock></CardContent></Card>
        <ChatGptMcpTunnel cfg={cfg} set={set} />
        <Card>
          <CardHeader><CardTitle className="flex items-center gap-2"><Bot className="h-4 w-4 text-primary" />Codex App Server</CardTitle><CardDescription>使用官方 openai/codex 完整包，或连接独立部署的官方 WebSocket App Server。</CardDescription></CardHeader>
          <CardContent className="grid gap-4 md:grid-cols-2">
            <Field label="连接方式" className="md:col-span-2"><SimpleSelect value={cfg.codex_app_mode ?? "internal"} onChange={(v) => set("codex_app_mode", v)} items={[["internal", "内部服务器（native）"], ["external", "外挂 WebSocket"]]} /></Field>
            {(cfg.codex_app_mode ?? "internal") === "internal" ? <>
              <Field label="Codex 二进制" className="md:col-span-2" hint="留空时自动解析或下载官方 Codex。"><Input className="font-mono text-xs" value={cfg.codex_command ?? ""} onChange={(e) => set("codex_command", e.target.value)} placeholder="自动解析官方 Codex" /></Field>
              <Field label="WebSocket 端口"><Input type="number" value={cfg.codex_ws_port ?? 8765} onChange={(e) => set("codex_ws_port", Number(e.target.value))} /></Field>
              <Field label="发行仓库"><Input value={cfg.codex_release_repo ?? "openai/codex"} onChange={(e) => set("codex_release_repo", e.target.value)} /></Field>
              <Field label="直接下载 URL" className="md:col-span-2" hint="可选；留空时自动选择当前平台的官方包。"><Input value={cfg.codex_download_url ?? ""} onChange={(e) => set("codex_download_url", e.target.value)} placeholder="https://github.com/openai/codex/releases/download/…/codex-package-….tar.gz" /></Field>
            </> : <>
              <Field label="WebSocket URL" className="md:col-span-2"><Input className="font-mono text-xs" value={cfg.codex_external_ws_url ?? ""} onChange={(e) => set("codex_external_ws_url", e.target.value)} placeholder="wss://host/codex" /></Field>
              <Field label="WebSocket 密钥" className="md:col-span-2" hint="连接外挂 App Server 的 Bearer 密钥。"><SecretSetting value={cfg.codex_external_ws_key} onChange={(v) => set("codex_external_ws_key", v)} placeholder="输入外挂 App Server 密钥" /></Field>
            </>}
            <div className="flex items-center justify-between rounded-lg border p-3"><div><Label>崩溃自动重启</Label><p className="mt-1 text-xs text-muted-foreground">异常退出后自动恢复</p></div><Switch checked={cfg.codex_auto_restart ?? true} onCheckedChange={(v) => set("codex_auto_restart", v)} /></div>
            <Alert>Codex agent / thread 已禁用，仅执行独立 RPC。</Alert>
            <Field label="审批请求策略"><SimpleSelect value={cfg.approval_policy ?? "on-request"} onChange={(v) => set("approval_policy", v)} items={[["untrusted", "严格请求"], ["on-request", "按需请求"], ["never", "从不请求"]]} /></Field>
            <Field label="默认系统访问"><SimpleSelect value={cfg.sandbox ?? "workspace-write"} onChange={(v) => set("sandbox", v)} items={[["read-only", "只读"], ["workspace-write", "工作区可写"], ["danger-full-access", "完全访问"]]} /></Field>
            <Field label="审批等待（毫秒）" hint="默认 300000（5 分钟）。"><Input type="number" min={1000} max={3600000} step={1000} value={cfg.approval_timeout_ms ?? 300000} onChange={(e) => set("approval_timeout_ms", Number(e.target.value))} /></Field>
          </CardContent>
        </Card>
        <Card><CardHeader><CardTitle className="flex items-center gap-2"><Network className="h-4 w-4 text-primary" />App Server 工具边界</CardTitle><CardDescription className="mt-1">Gateway 只暴露登记过的独立执行工具。</CardDescription></CardHeader><CardContent><Alert>未知副作用操作默认禁止，需经统一审批。</Alert></CardContent></Card>
        <McpTools />
      </div>
      <div className="space-y-4 xl:sticky xl:top-24 xl:h-fit"><Card><CardHeader><CardTitle>OAuth Metadata 自检</CardTitle><CardDescription>检查当前运行实例的 OAuth 配置是否完整。</CardDescription></CardHeader><CardContent className="space-y-3"><Badge variant={audit?.complete ? "success" : audit?.enabled ? "warning" : "secondary"}>{audit?.complete ? "配置完整" : audit?.enabled ? "需要修复" : "OAuth 未启用"}</Badge>{audit?.issues?.map((issue: string) => <p key={issue} className="text-xs leading-5 text-destructive">{issue}</p>)}</CardContent></Card><Alert tone="warning">认证类改动需重启 Gateway 才会生效；修改 Web Token 后需重新登录。</Alert></div>
    </div>
  </Page>;
}

function McpTools() {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [openServers, setOpenServers] = useState<Record<string, boolean>>({});
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({});
  const load = () => api.mcpTools("").then(setData).catch((e) => setError(String(e)));
  useEffect(() => { load(); }, []);
  async function setPolicy(key: string, value: string) {
    setBusy(key); setError("");
    try { await api.setMcpToolPolicy("", { [key]: value }); await load(); }
    catch (e) { setError(String(e)); } finally { setBusy(""); }
  }
  async function setGroupPolicy(server: string, tools: any[], value: string) {
    setBusy(`${server}/*`); setError("");
    const policies: Record<string, string> = {};
    for (const tool of tools) policies[`${server}/${tool.name}`] = value;
    try { await api.setMcpToolPolicy("", policies); await load(); }
    catch (e) { setError(String(e)); } finally { setBusy(""); }
  }
  const servers = data?.servers ?? [];
  const total = servers.reduce((n: number, s: any) => n + (s.tools?.length ?? 0), 0);
  const prefixOf = (name: string) => { const i = name.indexOf("."); return i > 0 ? name.slice(0, i) : ""; };
  const groupOf = (server: any) => {
    const byPrefix = new Map<string, any[]>();
    for (const tool of server.tools ?? []) {
      const prefix = prefixOf(tool.name);
      if (!byPrefix.has(prefix)) byPrefix.set(prefix, []);
      byPrefix.get(prefix)!.push(tool);
    }
    // Only split into prefix groups when there's more than one meaningful prefix;
    // a single/prefix-less server renders as one flat group.
    const entries = [...byPrefix.entries()];
    const meaningful = entries.filter(([p]) => p !== "");
    return entries.length > 1 && meaningful.length > 0 ? entries : [["", server.tools ?? []]];
  };
  const toolRow = (server: any, tool: any) => {
    const key = `${server.name}/${tool.name}`;
    const policy = tool.policy ?? "deny";
    return <div key={key} className="flex items-center gap-3 px-3 py-2"><div className="min-w-0 flex-1"><div className="flex items-center gap-2"><span className="truncate font-mono text-xs">{tool.name}</span>{tool.readOnly ? <Badge variant="success">只读</Badge> : <Badge variant="warning">副作用</Badge>}</div>{tool.description && <p className="mt-0.5 truncate text-xs text-muted-foreground">{tool.description}</p>}</div><div className="flex shrink-0 gap-1">{[["allow", "允许"], ["ask", "需审批"], ["deny", "禁止"]].map(([value, label]) => <button key={value} disabled={busy === key} onClick={() => setPolicy(key, value)} className={cn("rounded-md border px-2 py-1 text-xs transition", policy === value ? "border-primary bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent")}>{label}</button>)}</div></div>;
  };
  return <Card><CardHeader><CardTitle className="flex items-center gap-2"><Network className="h-4 w-4 text-primary" />下游 MCP 工具</CardTitle><CardDescription>选择哪些下游 MCP 工具暴露给 WebChat。未列出的工具默认禁止；只读工具可直接运行，有副作用的一律需一次性审批。</CardDescription></CardHeader><CardContent className="space-y-3">
    {error && <Alert>{error}</Alert>}
    {!data ? <Loading text="正在读取 MCP 工具" /> : servers.length === 0 ? <Empty icon={Network} title="没有可用的下游 MCP 工具" detail="在 Codex 配置里登记下游 MCP server 后，这里会列出它们提供的工具。" /> : <>
      <p className="text-xs text-muted-foreground">{servers.length} 个 server · {total} 个工具</p>
      {servers.map((server: any) => {
        const serverOpen = openServers[server.name] ?? (server.tools?.length ?? 0) <= 10;
        const sideEffects = (server.tools ?? []).filter((t: any) => !t.readOnly).length;
        return <div key={server.name} className="rounded-lg border">
          <button type="button" onClick={() => setOpenServers((now) => ({ ...now, [server.name]: !serverOpen }))} className="flex w-full items-center gap-2 border-b bg-muted/40 px-3 py-2 text-left">
            <ChevronRight className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform", serverOpen && "rotate-90")} />
            <span className="text-sm font-medium">{server.name}</span>
            <span className="ml-auto text-xs text-muted-foreground">{server.tools?.length ?? 0} 个工具{sideEffects > 0 ? ` · ${sideEffects} 副作用` : ""}</span>
          </button>
          {serverOpen && <div>{groupOf(server).map(([prefix, tools]) => {
            const groupKey = `${server.name}::${prefix}`;
            const groupOpen = openGroups[groupKey] ?? ((server.tools?.length ?? 0) <= 10 || prefix === "");
            const sideCount = tools.filter((t: any) => !t.readOnly).length;
            return <div key={groupKey} className="border-b last:border-b-0">
              {prefix !== "" && <div className="flex items-center gap-2 bg-muted/20 px-3 py-1.5">
                <button type="button" onClick={() => setOpenGroups((now) => ({ ...now, [groupKey]: !groupOpen }))} className="flex items-center gap-2 text-left">
                  <ChevronRight className={cn("h-3.5 w-3.5 text-muted-foreground transition-transform", groupOpen && "rotate-90")} />
                  <span className="font-mono text-xs font-medium">{prefix}.*</span>
                  <span className="text-[11px] text-muted-foreground">{tools.length}{sideCount > 0 ? ` · ${sideCount} 副作用` : ""}</span>
                </button>
                <div className="ml-auto flex gap-1">{[["allow", "全允许"], ["deny", "全禁止"]].map(([value, label]) => <button key={value} disabled={busy === `${server}/*`} onClick={() => setGroupPolicy(server.name, tools, value)} className="rounded border px-1.5 py-0.5 text-[11px] text-muted-foreground transition hover:bg-accent">{label}</button>)}</div>
              </div>}
              {groupOpen && <div className="divide-y">{tools.map((tool: any) => toolRow(server, tool))}</div>}
            </div>;
          })}</div>}
        </div>;
      })}
    </>}
  </CardContent></Card>;
}

function Approvals() {  const [items, setItems] = useState<any[]>([]); const load = () => api.approvals("").then((d) => setItems(d.pending ?? [])).catch(() => {});
  useEffect(() => {
    load();
    const stream = new EventSource(api.eventStreamUrl("*"), { withCredentials: true });
    const refresh = () => load();
    for (const name of ["approval.created", "approval.updated", "approval.resolved", "operation.completed", "operation.failed"]) {
      stream.addEventListener(name, refresh);
    }
    const fallback = setInterval(load, 30000);
    return () => { stream.close(); clearInterval(fallback); };
  }, []);
  async function reply(item: any, decision: string) {
    await api.resolveApproval("", item.requestId, decision, item.version, item.conversationId);
    load();
  }
  return <Page title="审批队列" description="处理需要授权的操作。">{!items.length ? <Card><CardContent className="p-6"><Empty icon={ShieldCheck} title="审批队列为空" detail="需要决策的操作会通过实时事件流出现在这里。" /></CardContent></Card> : <div className="grid gap-4 lg:grid-cols-2">{items.map((item) => <Card key={item.requestId}><CardHeader><div className="flex items-center gap-2"><Badge variant="warning">{item.kind}</Badge><Badge variant="secondary">{item.source === "appserver" ? "App Server 原生" : "Gateway 补位"}</Badge><span className="text-xs text-muted-foreground">{item.method}</span></div><CardTitle className="mt-3">{item.source === "appserver" ? "App Server 请求授权" : "独立操作请求授权"}</CardTitle><CardDescription>{item.conversationId} · v{item.version}</CardDescription></CardHeader><CardContent><pre className="max-h-52 overflow-auto rounded-lg bg-muted p-3 text-xs leading-5">{JSON.stringify(item.params, null, 2).slice(0, 2000)}</pre><div className="mt-4 flex flex-wrap gap-2"><Button size="sm" onClick={() => reply(item, item.source === "appserver" ? "accept" : "approve_once")}>允许一次</Button>{item.availableDecisions?.includes("acceptForSession") && <Button size="sm" variant="outline" onClick={() => reply(item, "acceptForSession")}>本会话允许</Button>}<Button size="sm" variant="ghost" className="text-destructive" onClick={() => reply(item, "decline")}>拒绝</Button></div></CardContent></Card>)}</div>}</Page>;
}

function Codex() {
  const [state, setState] = useState<any>(null);
  const [nativeState, setNativeState] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [archivePath, setArchivePath] = useState("");
  const [error, setError] = useState("");
  const load = () => { api.appserverStatus("").then(setState).catch(() => {}); api.nativeStatus("").then(setNativeState).catch(() => {}); };
  useEffect(() => { load(); const timer = setInterval(load, 3000); return () => clearInterval(timer); }, []);
  async function restart() { setBusy(true); setError(""); try { await api.appserverRestart(""); load(); } catch (e) { setError(String(e)); } finally { setBusy(false); } }
  async function install() { setInstalling(true); setError(""); try { await api.installCodex("", { archivePath: archivePath.trim() }); load(); } catch (e) { setError(String(e)); } finally { setInstalling(false); } }
  const capabilityText = state?.apiCapabilities
    ? Object.entries(state.apiCapabilities).map(([name, enabled]) => `${name}: ${enabled ? "✓" : "×"}`).join(" · ")
    : "尚未检测";
  return <Page title="Codex Runtime" description="管理官方 Codex App Server 进程。"><div className="grid gap-6 xl:grid-cols-[1fr_.7fr]"><Card><CardHeader className="flex-row items-center gap-4"><div className={cn("rounded-xl p-3", state?.running ? "bg-emerald-500/10 text-emerald-600" : "bg-muted text-muted-foreground")}><Server className="h-5 w-5" /></div><div><CardTitle>{state?.running ? "App Server 正在运行" : "App Server 已停止"}</CardTitle><CardDescription>{state?.mode ?? "internal"} · PID {state?.pid ?? "—"} · 运行 {state?.uptimeSec ?? 0}s</CardDescription></div><Badge variant={state?.apiCompatible === true ? "success" : state?.apiCompatible === false ? "warning" : "secondary"} className="ml-auto">官方 API {state?.apiCompatible === true ? "可用" : state?.apiCompatible === false ? "不兼容" : "待检测"}</Badge></CardHeader><CardContent>{state?.apiWarning && <Alert tone="warning">{state.apiWarning}</Alert>}{error && <Alert>{error}</Alert>}<Details data={[["User agent", state?.userAgent], ["Platform", state?.platformOs || state?.platformFamily], ["API", capabilityText], ["执行模式", state?.executionCapabilities?.codexAgentSessions === false ? "无 Codex agent 会话" : "未知"], ["审批", state?.executionCapabilities?.fallbackApproval ? "App Server 原生优先 / Gateway 补位" : "不可用"], ["Exec policy", state?.executionCapabilities?.execPolicyMode], ["独立文件 RPC", state?.executionCapabilities?.standaloneFilesystem], ["远端路径边界", state?.executionCapabilities?.remoteFilesystemBoundary], ["事件", state?.executionCapabilities?.eventTransport], ["Listen", state?.listen], ["Codex home", state?.codexHome], ["Target", nativeState?.codexTarget], ["Native", nativeState?.codexCommand || "未安装"], ["重启次数", state?.restartCount ?? 0]]} /><div className="mt-5 flex flex-wrap gap-2"><Button onClick={restart} disabled={busy}>{busy ? <RefreshCw className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}重启 / 重连</Button><Button variant="outline" onClick={() => api.appserverStop("").then(load)}><Square className="h-3.5 w-3.5" />停止</Button></div><div className="mt-5 space-y-3 rounded-lg border p-3"><Field label="本地官方完整包（可选）" hint="留空时自动下载当前平台的官方 Codex 包。"><Input value={archivePath} onChange={(e) => setArchivePath(e.target.value)} placeholder="C:\path\codex-package-x86_64-pc-windows-msvc.tar.gz" className="font-mono text-xs" /></Field><Button variant="outline" onClick={install} disabled={installing}>{installing ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}安装 / 更新官方 Codex</Button></div></CardContent></Card></div></Page>;
}

function Brand({ inverse = false }: { inverse?: boolean }) { return <div className={cn("flex h-16 items-center gap-3 px-5", inverse && "px-0")}><div className="flex h-9 w-9 items-center justify-center rounded-xl bg-primary text-primary-foreground shadow-sm"><Bot className="h-5 w-5" /></div><div><p className={cn("text-sm font-semibold tracking-tight", inverse && "text-white")}>ChatCodex</p><p className={cn("text-[11px] text-muted-foreground", inverse && "text-zinc-400")}>Local agent gateway</p></div></div>; }
function Splash() { return <div className="flex min-h-screen items-center justify-center bg-background"><div className="text-center"><div className="mx-auto flex h-11 w-11 items-center justify-center rounded-xl bg-primary text-primary-foreground"><Bot className="h-5 w-5" /></div><RefreshCw className="mx-auto mt-5 h-4 w-4 animate-spin text-muted-foreground" /></div></div>; }
function Page({ title, description, actions, children }: { title: string; description: string; actions?: React.ReactNode; children: React.ReactNode }) { return <section><div className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between"><div><h1 className="text-2xl font-semibold tracking-tight">{title}</h1><p className="mt-1 text-sm text-muted-foreground">{description}</p></div>{actions}</div>{children}</section>; }
function Field({ label, hint, className, children }: { label: string; hint?: string; className?: string; children: React.ReactNode }) { return <div className={cn("space-y-2", className)}><Label>{label}</Label>{children}{hint && <p className="text-xs leading-5 text-muted-foreground">{hint}</p>}</div>; }
function SecretInput({ value, setValue, placeholder }: { value: string; setValue(v: string): void; placeholder: string }) { const [show, setShow] = useState(false); return <div className="relative"><Input type={show ? "text" : "password"} value={value} onChange={(e) => setValue(e.target.value)} placeholder={placeholder} className="pr-10 font-mono text-xs" /><Button type="button" variant="ghost" size="icon" className="absolute right-0 top-0 h-9 w-9" onClick={() => setShow(!show)}>{show ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}</Button></div>; }
function SecretSetting({ value, onChange, placeholder }: { value: string; onChange(v: string): void; placeholder: string }) { const configured = value === "********"; return <SecretInput value={configured ? "" : (value ?? "")} setValue={onChange} placeholder={configured ? "已配置 · 留空保持现值" : placeholder} />; }
function SimpleSelect({ value, onChange, items }: { value: string; onChange(v: string): void; items: string[][] }) { return <Select value={value} onValueChange={onChange}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{items.map(([v, label]) => <SelectItem key={v} value={v}>{label}</SelectItem>)}</SelectContent></Select>; }
function SecurityBlock({ number, title, description, children }: { number: string; title: string; description: string; children: React.ReactNode }) { return <div className="grid gap-4 md:grid-cols-[180px_1fr]"><div><span className="font-mono text-xs text-primary">{number}</span><p className="mt-1 text-sm font-semibold">{title}</p><p className="mt-1 text-xs leading-5 text-muted-foreground">{description}</p></div><div>{children}</div></div>; }
function Boundary({ icon: Icon, title, value, detail }: { icon: React.ElementType; title: string; value: string; detail: string }) { return <div className="flex items-center gap-3 rounded-lg border p-3"><Icon className="h-4 w-4 text-primary" /><div className="min-w-0"><p className="text-sm font-medium">{title} <span className="text-muted-foreground">· {value}</span></p><p className="text-xs text-muted-foreground">{detail}</p></div></div>; }
function StatusLine({ label, on }: { label: string; on?: boolean }) { return <div className="flex items-center gap-2 py-1 text-xs"><span className={cn("h-2 w-2 rounded-full", on ? "bg-emerald-500" : "bg-muted-foreground/40")} /><span className="text-muted-foreground">{label}</span><span className="ml-auto font-medium">{on ? "在线" : "离线"}</span></div>; }
function MiniStatus({ label, on }: { label: string; on: boolean }) { return <div className="rounded-lg border p-2 text-center"><div className={cn("mx-auto mb-1 h-1.5 w-1.5 rounded-full", on ? "bg-emerald-500" : "bg-muted-foreground/40")} /><p className="text-[11px] text-muted-foreground">{label}</p></div>; }
function Details({ data }: { data: Array<[string, any]> }) { return <dl className="space-y-3">{data.map(([key, value]) => <div key={key} className="grid grid-cols-[90px_1fr] gap-3 text-sm"><dt className="text-muted-foreground">{key}</dt><dd className="break-all font-mono text-xs leading-5">{value ?? "—"}</dd></div>)}</dl>; }
function CopyValue({ value }: { value: string }) { const [copied, setCopied] = useState(false); return <div className="flex items-center gap-2 rounded-lg border bg-muted/40 p-2 pl-3"><code className="min-w-0 flex-1 break-all text-xs">{value}</code><Button size="icon" variant="ghost" className="shrink-0" onClick={async () => { await navigator.clipboard.writeText(value); setCopied(true); setTimeout(() => setCopied(false), 1200); }}>{copied ? <Check className="h-4 w-4 text-emerald-600" /> : <Copy className="h-4 w-4" />}</Button></div>; }
function Empty({ icon: Icon, title, detail, action }: { icon: React.ElementType; title: string; detail: string; action?: React.ReactNode }) { return <div className="flex flex-col items-center px-4 py-8 text-center"><div className="mb-3 rounded-full bg-muted p-3 text-muted-foreground"><Icon className="h-5 w-5" /></div><p className="text-sm font-medium">{title}</p><p className="mt-1 max-w-sm text-xs leading-5 text-muted-foreground">{detail}</p>{action && <div className="mt-4">{action}</div>}</div>; }
function Alert({ tone = "error", children }: { tone?: "error" | "warning"; children: React.ReactNode }) { return <div role="alert" className={cn("mb-4 rounded-lg border p-3 text-sm leading-6", tone === "warning" ? "border-amber-500/20 bg-amber-500/5 text-amber-800 dark:text-amber-200" : "border-destructive/20 bg-destructive/5 text-destructive")}>{children}</div>; }
function Loading({ text }: { text: string }) { return <div className="flex items-center gap-2 text-sm text-muted-foreground"><RefreshCw className="h-4 w-4 animate-spin" />{text}</div>; }
const mcpUrl = (url: string) => `${url.replace(/\/+$/, "")}/mcp`;

createRoot(document.getElementById("root")!).render(<App />);
