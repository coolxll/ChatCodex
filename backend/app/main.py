"""ChatCodex Gateway.

FastMCP owns the MCP transport. WebChat owns the conversation and model loop;
Gateway owns thread-free execution contexts, policy, approval and standalone
Codex App Server RPCs.

入口:uvicorn app.main:app  (或 python -m app.main)
"""
from __future__ import annotations

import atexit
import asyncio
from dataclasses import replace
import hmac
import json
import re
import secrets
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from .appserver import AppServerManager
from .approval import ApprovalBridge
from .config import Settings, load_settings
from .db import Database
from .events import EventBroker
from .execution import ExecutionOrchestrator, ExecutionError
from .mcp_server import build_mcp, update_widget_domains
from .models import ExecutionRegistry
from .oauth import Authenticator, Principal, WebAuthenticator
from .oauth import is_chatgpt_connector_callback
from .native import NativeRuntimeManager, NativeRuntimeError
from .settings_store import SettingsStore
from .tunnel import TunnelManager

settings = load_settings()
db = Database(settings)
atexit.register(db.close)
execution_registry = ExecutionRegistry(db)
settings_store = SettingsStore(db)
native = NativeRuntimeManager(settings.native_dir)


def _runtime(key: str, env_fallback):
    """运行时设置(web 管理页)> 环境变量兜底。"""
    v = settings_store.get_override(key)
    if v is None or v == "":
        return env_fallback
    return v


# Web 与 MCP token:运行时设置 > 新环境变量 > 旧配置迁移 > 自动生成。
_legacy_token = settings_store.get_override("auth_token") or ""
_web_access_token = _runtime(
    "web_access_token", settings.web_access_token or _legacy_token)
_mcp_access_token = _runtime(
    "mcp_access_token", settings.mcp_access_token)
_legacy_mode = settings_store.get_override("auth_mode") or settings.mcp_auth_mode
_mcp_auth_mode = _runtime("mcp_auth_mode", _legacy_mode)
_mcp_auth_mode = {"bearer": "token"}.get(str(_mcp_auth_mode), str(_mcp_auth_mode))
if _mcp_auth_mode not in {"token", "oauth", "both", "noauth"}:
    raise RuntimeError("CHATCODEX_MCP_AUTH_MODE must be token, oauth, both, or noauth")

if not _web_access_token:
    _web_access_token = secrets.token_urlsafe(24)
    settings_store.set("web_access_token", _web_access_token)
    _GENERATED_WEB_TOKEN = True
else:
    _GENERATED_WEB_TOKEN = False
if not _mcp_access_token:
    _mcp_access_token = secrets.token_urlsafe(24)
    settings_store.set("mcp_access_token", _mcp_access_token)
    _GENERATED_MCP_TOKEN = True
else:
    _GENERATED_MCP_TOKEN = False

_internal_ws_key = _runtime(
    "codex_internal_ws_key", settings.codex_internal_ws_key)
if not _internal_ws_key:
    _internal_ws_key = secrets.token_urlsafe(48)
    settings_store.set("codex_internal_ws_key", _internal_ws_key)

# OAuth signing material must be stable across restarts. Replace the packaged
# development sentinel with a random persisted secret on first launch.
_oauth_token_secret = _runtime("oauth_token_secret", settings.oauth_token_secret)
if not _oauth_token_secret or _oauth_token_secret == "dev-secret-change-me":
    _oauth_token_secret = secrets.token_urlsafe(48)
    settings_store.set("oauth_token_secret", _oauth_token_secret)
_oauth_password = _runtime("oauth_password", settings.oauth_password) or _web_access_token

_PUBLIC_ROUTE_KINDS = {"direct", "cloudflared-try", "cloudflared-named"}
_PUBLIC_ROUTE_INSTANCE = "public-route"
_CHATGPT_MCP_INSTANCE = "chatgpt-mcp"
_legacy_route = settings_store.get_override("tunnel_kind")
_configured_route = settings_store.get_override("public_route_kind")
_public_route_kind = (
    _configured_route if _configured_route in _PUBLIC_ROUTE_KINDS
    else _legacy_route if _legacy_route in _PUBLIC_ROUTE_KINDS
    else settings.public_route_kind if settings.public_route_kind in _PUBLIC_ROUTE_KINDS
    else ""
)
_public_url = str(_runtime("public_url", settings.public_url)).rstrip("/")

# 认证:运行时设置优先
settings = Settings(**{**settings.__dict__,
                       "web_access_token": _web_access_token,
                       "mcp_auth_mode": _mcp_auth_mode,
                       "mcp_access_token": _mcp_access_token,
                       "oauth_token_secret": _oauth_token_secret,
                       "oauth_password": _oauth_password,
                       "oauth_callback_protection": bool(_runtime(
                           "oauth_callback_protection",
                           settings.oauth_callback_protection)),
                       "public_url": _public_url,
                       "public_route_kind": _public_route_kind,
                       "tunnel_kind": _public_route_kind,
                       "chatgpt_tunnel_enabled": bool(_runtime(
                           "chatgpt_tunnel_enabled",
                           settings.chatgpt_tunnel_enabled)),
                       "chatgpt_tunnel_id": _runtime(
                           "chatgpt_tunnel_id", settings.chatgpt_tunnel_id),
                       "codex_app_mode": _runtime(
                           "codex_app_mode", settings.codex_app_mode),
                       "codex_command": _runtime("codex_command", settings.codex_command),
                       "codex_external_ws_url": _runtime(
                           "codex_external_ws_url", settings.codex_external_ws_url),
                       "codex_external_ws_key": _runtime(
                           "codex_external_ws_key", settings.codex_external_ws_key),
                       "codex_internal_ws_key": _internal_ws_key,
                       "codex_release_repo": _runtime(
                           "codex_release_repo", settings.codex_release_repo),
                       "codex_download_url": _runtime(
                           "codex_download_url", settings.codex_download_url),
                       "tunnel_client_command": _runtime(
                           "tunnel_client_command", settings.tunnel_client_command),
                       "tunnel_client_release": _runtime(
                           "tunnel_client_release", settings.tunnel_client_release),
                       "tunnel_auto_restart": bool(_runtime(
                           "tunnel_auto_restart", settings.tunnel_auto_restart))})
auth = Authenticator(settings, db=db)
web_auth = WebAuthenticator(settings.web_access_token)
appserver = AppServerManager(
    settings,
    port=int(_runtime("codex_ws_port", settings.codex_ws_port)),
    auto_restart=bool(_runtime("codex_auto_restart", True)),
    native=native)
tunnels = TunnelManager(settings, native=native)


events = EventBroker()
approval = ApprovalBridge(
    appserver,
    db,
    events=events,
    approval_timeout_ms=int(_runtime(
        "approval_timeout_ms", settings.approval_timeout_ms
    )),
)
orch = ExecutionOrchestrator(
    settings, appserver, execution_registry, settings_store)
mcp = build_mcp(settings, orch, approval, auth)


def _reset_codex_runtime() -> None:
    """Invalidate pending operations and approval-bound context versions."""
    conversations = {
        context.conversation_id
        for context in execution_registry.list(status="active")
    }
    approval.cancel_pending()
    execution_registry.invalidate_appserver_instance()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for conversation_id in conversations:
        loop.create_task(events.publish(
            "runtime.degraded",
            conversation_id,
            {
                "reason": "appserver_reset",
                "message": (
                    "App Server changed; review and save the execution "
                    "workspace before running another mutation."
                ),
            },
        ))


class _ReloadableAsgi:
    """Atomically swap generated MCP auth routes without dropping sessions."""

    def __init__(self, target):
        self.target = target

    def replace(self, target) -> None:
        self.target = target

    async def __call__(self, scope, receive, send):
        target = self.target
        await target(scope, receive, send)


# The session manager belongs to FastMCP and is reused by every generated
# Starlette wrapper. Only auth discovery URLs/middleware need replacement when
# a TryCloudflare hostname becomes known after process startup.
_mcp_asgi = _ReloadableAsgi(mcp.streamable_http_app())


def _valid_runtime_public_root(public_url: str, *, https_required: bool) -> bool:
    try:
        parsed = urlsplit(public_url)
    except ValueError:
        return False
    if (parsed.scheme not in ({"https"} if https_required else {"http", "https"})
            or not parsed.hostname or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        return False
    if https_required and parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
        return False
    return True


def _replace_runtime_public_url(public_url: str, *, https_required: bool) -> bool:
    """Update all issuer/resource consumers for this running process."""
    base = str(public_url or "").rstrip("/")
    if not _valid_runtime_public_root(base, https_required=https_required):
        return False
    if auth.public_url == base:
        return True
    auth.set_public_url(base)
    update_widget_domains(mcp, base)
    if mcp.settings.auth is not None:
        mcp.settings.auth = mcp.settings.auth.model_copy(update={
            "issuer_url": base,
            "resource_server_url": f"{base}/mcp",
        })
        _mcp_asgi.replace(mcp.streamable_http_app())
    tunnels.settings = replace(tunnels.settings, public_url=base)
    print(f"[chatcodex] runtime OAuth public URL: {base}", flush=True)
    return True


def _activate_tunnel_public_url(public_url: str) -> None:
    if not _replace_runtime_public_url(public_url, https_required=True):
        raise ValueError("tunnel public URL must be a public HTTPS root URL")


tunnels.on_public_url = _activate_tunnel_public_url


@asynccontextmanager
async def lifespan(app: FastAPI):
    async def on_server_request(msg: dict) -> dict:
        # Unknown reverse RPCs must fail explicitly. Returning an empty success
        # can make App Server believe a security- or auth-sensitive client
        # capability was handled when the Gateway never implemented it.
        return await approval.handle(msg)

    appserver.on_server_request(on_server_request)
    appserver.on_reset(_reset_codex_runtime)
    _print_startup_banner()
    print("[chatcodex] spawning codex app-server ...", flush=True)
    try:
        await appserver.start()
        print(f"[chatcodex] codex ready: {appserver.initialize_result.get('userAgent','?')}", flush=True)
    except Exception as exc:
        # Keep the admin surface alive so a first-run user can download the
        # native runtime or configure an external WebSocket endpoint.
        print(f"[chatcodex] codex unavailable: {exc}", flush=True)
    # 全局公网入口与 ChatGPT MCP Tunnel 是两个独立生命周期。
    await _autostart_transports()
    # FastMCP session manager 需在 run() 上下文中(stateless 也要)
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        await tunnels.stop()
        await appserver.stop()
        db.close()


async def _autostart_transports() -> None:
    if not appserver.status().get("running"):
        print("[chatcodex] transport autostart skipped: Codex App Server is unavailable")
        return
    route = settings.public_route_kind.strip()
    if route:
        try:
            if route == "cloudflared-try":
                st = await tunnels.start(
                    "cloudflared", mode="try", instance_id=_PUBLIC_ROUTE_INSTANCE)
            elif route == "cloudflared-named":
                st = await tunnels.start(
                    "cloudflared", mode="named", token=settings.cloudflared_token,
                    instance_id=_PUBLIC_ROUTE_INSTANCE)
            else:
                st = await tunnels.start(
                    "direct", instance_id=_PUBLIC_ROUTE_INSTANCE)
            print(f"[chatcodex] public route {route}: running={st.get('running')} url={st.get('url')}")
        except Exception as exc:
            print(f"[chatcodex] public route autostart failed: {exc}")
    if settings.chatgpt_tunnel_enabled:
        try:
            st = await tunnels.start(
                "chatgpt", tunnel_id=settings.chatgpt_tunnel_id,
                api_key=settings.chatgpt_api_key,
                client_bin=settings.tunnel_client_command,
                instance_id=_CHATGPT_MCP_INSTANCE)
            print(f"[chatcodex] ChatGPT MCP tunnel: running={st.get('running')} ready={st.get('ready')}")
        except Exception as exc:
            print(f"[chatcodex] ChatGPT MCP tunnel autostart failed: {exc}")


def _print_startup_banner() -> None:
    """启动横幅:分别显示 Web 与 MCP 认证边界。"""
    if _GENERATED_WEB_TOKEN:
        print(f"[chatcodex] generated web access token (save it now): "
              f"{settings.web_access_token}", flush=True)
    else:
        print("[chatcodex] web access token: configured (value hidden)", flush=True)
    print(f"[chatcodex] mcp auth mode: {settings.mcp_auth_mode}", flush=True)
    if settings.mcp_auth_mode in ("token", "both"):
        if _GENERATED_MCP_TOKEN:
            print(f"[chatcodex] generated mcp access token (save it now): "
                  f"{settings.mcp_access_token}", flush=True)
        else:
            print("[chatcodex] mcp access token: configured (value hidden)", flush=True)
    print(f"[chatcodex] admin panel: http://{settings.host}:{settings.port}/", flush=True)
    print(f"[chatcodex] mcp endpoint: http://{settings.host}:{settings.port}/mcp/", flush=True)


app = FastAPI(title="ChatCodex Gateway", version="0.1.0", lifespan=lifespan)


_WEB_COOKIE = "chatcodex_web_session"
_SAFE_HTTP_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def _origin_tuple(value: str) -> Optional[tuple[str, str, int]]:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.lower(), port


def _request_origin(request: Request) -> Optional[tuple[str, str, int]]:
    proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    return _origin_tuple(
        f"{proto or request.url.scheme}://{host or request.headers.get('host', '')}")


def web_principal(authorization: Optional[str] = Header(default=None),
                  request: Request = None) -> Principal:
    token = ""
    cookie_authenticated = False
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif request:
        token = request.cookies.get(_WEB_COOKIE, "")
        cookie_authenticated = bool(token)
    p = web_auth.authenticate(token)
    if not p:
        raise HTTPException(status_code=401, detail="unauthorized",
                            headers={"WWW-Authenticate": 'Bearer realm="ChatCodex Web"'})
    if (cookie_authenticated and request.method.upper() not in _SAFE_HTTP_METHODS
            and _origin_tuple(request.headers.get("origin", "")) != _request_origin(request)):
        raise HTTPException(status_code=403, detail="cross-origin admin request rejected")
    return p


# Existing route signatures use this dependency name; it now represents only
# the Web administration boundary and is never used by the mounted MCP app.
principal = web_principal


@app.get("/healthz")
async def healthz():
    st = appserver.status()
    return {"ok": st.get("healthy", False), "appserver": st.get("running", False),
            "healthy": st.get("healthy", False),
            "auth": {"web": "token", "mcp": settings.mcp_auth_mode}}


# ---------- Web 管理面板(静态 SPA) ----------
@app.get("/")
@app.get("/panel")
async def panel():
    import os
    from fastapi.responses import FileResponse
    index = os.path.join(settings.frontend_dist, "panel", "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"service": "chatcodex", "mcp": "/mcp", "healthz": "/healthz",
            "hint": "frontend 未构建:cd frontend && npm install && npm run build"}


@app.post("/api/auth/session")
async def create_web_session(request: Request):
    """Exchange the Web Access Token for an HttpOnly same-site browser cookie."""
    try:
        body = await _read_json_body_limited(request, 8 * 1024)
    except HTTPException:
        raise
    except Exception:
        body = {}
    if not web_auth.authenticate(str(body.get("token", ""))):
        raise HTTPException(401, "invalid Web Access Token")
    response = JSONResponse({"authenticated": True})
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    response.set_cookie(
        _WEB_COOKIE, settings.web_access_token, httponly=True,
        secure=request.url.scheme == "https" or forwarded_proto == "https",
        samesite="strict", path="/",
    )
    return response


@app.get("/api/auth/session")
async def web_session_status(p: Principal = Depends(web_principal)):
    return {"authenticated": True, "user": p.user_id}


@app.delete("/api/auth/session")
async def delete_web_session():
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(_WEB_COOKIE, path="/")
    return response


# FastMCP 子应用挂在 /mcp(鉴权由 FastMCP 原生 token_verifier 处理,无需中间件)
app.mount("/mcp", _mcp_asgi)


# ---------- OAuth 2.1 ----------
def _protected_resource_metadata() -> dict:
    return {
        "resource": auth.resource,
        "authorization_servers": [auth.public_url],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["codex"],
    }


def _authorization_server_metadata() -> dict:
    base = auth.public_url
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "response_types_supported": ["code"],
        "scopes_supported": ["codex"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


def _oauth_metadata_audit() -> dict:
    issues: list[str] = []
    try:
        parsed = urlsplit(auth.public_url)
    except ValueError:
        parsed = None
    if not parsed or parsed.scheme != "https" or not parsed.hostname:
        issues.append("OAuth issuer 必须是可公网访问的 HTTPS 根地址")
    elif parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
        issues.append("OAuth issuer 不能是 loopback 地址")
    if parsed and (parsed.query or parsed.fragment or parsed.username or parsed.password):
        issues.append("OAuth issuer 不能包含认证信息、query 或 fragment")
    if parsed and parsed.path not in {"", "/"}:
        issues.append("当前内置 OAuth 仅支持站点根路径 issuer")
    return {
        "enabled": settings.mcp_auth_mode in {"oauth", "both"},
        "complete": settings.mcp_auth_mode in {"oauth", "both"} and not issues,
        "issues": issues,
        "registrationMode": "dcr",
        "pkce": "S256",
        "protectedResource": _protected_resource_metadata(),
        "authorizationServer": _authorization_server_metadata(),
        "publicUrlSource": (
            "cloudflared-runtime"
            if auth.public_url != settings.public_url else "configured"
        ),
        "note": (
            "Secure MCP Tunnel rewrites resource/resource_metadata to its public "
            "endpoint; authorization_servers[0] remains this public HTTPS issuer."
        ),
    }


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
async def opr():
    _require_oauth_mode()
    return _protected_resource_metadata()


@app.get("/.well-known/oauth-authorization-server")
async def oas():
    _require_oauth_mode()
    return _authorization_server_metadata()


@app.post("/oauth/register")
async def oauth_register(request: Request):
    _require_oauth_mode()
    try:
        return auth.store.register_client(
            await _read_json_body_limited(request, 32 * 1024) or {})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


async def _read_json_body_limited(request: Request, max_bytes: int) -> dict:
    """Read a small public JSON request without allowing unbounded buffering."""
    body = await _read_body_limited(request, max_bytes)
    try:
        value = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "invalid JSON body") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "JSON body must be an object")
    return value


async def _read_body_limited(request: Request, max_bytes: int) -> bytes:
    """Read a public request body with both declared and streaming limits."""
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > max_bytes:
                raise HTTPException(413, "request body is too large")
        except ValueError:
            raise HTTPException(400, "invalid content-length")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise HTTPException(413, "request body is too large")
    return bytes(body)


# 授权同意页(ChatGPT 风格 + 可选密码)
_CONSENT_HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>授权 · ChatCodex</title>
<style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}
body{{font-family:ui-sans-serif,system-ui,-apple-system,'Segoe UI',Roboto;margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh;background:#fff;color:#0d0d0d}}
.card{{width:100%;max-width:400px;padding:40px 32px}}
.logo{{display:flex;align-items:center;gap:10px;margin-bottom:28px}}
.logo .mark{{width:34px;height:34px;border-radius:9px;background:#10a37f;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:17px}}
.logo .name{{font-size:17px;font-weight:600}}
h1{{font-size:22px;margin:0 0 8px;letter-spacing:-.01em}}
.sub{{color:#6e6e6e;font-size:14px;margin:0 0 22px;line-height:1.5}}
.client,.scope{{background:#f7f7f8;border:1px solid #ececf1;border-radius:10px;padding:12px 14px;font-size:13px;margin-bottom:14px}}
.scope{{display:flex;gap:8px;align-items:flex-start}}
.scope .dot{{color:#10a37f;margin-top:1px}}
label{{display:block;font-size:13px;font-weight:600;margin:0 0 6px}}
input[type=password]{{width:100%;padding:11px 13px;border:1px solid #d9d9e3;border-radius:9px;font-size:14px;outline:none;margin-bottom:6px}}
input[type=password]:focus{{border-color:#10a37f;box-shadow:0 0 0 3px rgba(16,163,127,.15)}}
.err{{color:#d92d20;font-size:13px;min-height:18px;margin-bottom:6px}}
button{{width:100%;padding:12px;border:0;border-radius:9px;background:#10a37f;color:#fff;font-size:15px;font-weight:600;cursor:pointer}}
button:hover{{background:#0e8f6f}}
.deny{{background:transparent;color:#6e6e6e;border:1px solid #d9d9e3;margin-top:10px}}
.deny:hover{{background:#f7f7f8}}
.foot{{text-align:center;color:#9b9b9b;font-size:12px;margin-top:22px}}
</style></head><body>
<div class=card>
  <div class=logo><div class=mark>C</div><div class=name>ChatCodex</div></div>
  <h1>授权访问 Codex</h1>
  <p class=sub>应用请求连接到你的 Codex 工作区,以读写文件、运行命令。</p>
  <div class=client>应用:<b>{client}</b></div>
  <div class=scope><span class=dot>●</span><span>权限范围:{scope}</span></div>
  <form method=post action="/oauth/authorize">{hidden}{password_field}<div class=err>{error}</div>
    <button type=submit>允许并继续</button></form>
  <form method=post action="/oauth/authorize">{hidden}<input type=hidden name=deny value=1>
    <button type=submit class=deny>拒绝</button></form>
  <div class=foot>授权即代表你信任此应用访问 Codex 工作区</div>
</div></body></html>"""


def _consent_page(client_id, scope, hidden, need_password, error=""):
    import html
    pf = ('<label for=pw>访问密码</label>'
          '<input id=pw type=password name=password placeholder="输入访问密码" required autofocus>'
          ) if need_password else ""
    return _CONSENT_HTML.format(
        client=html.escape(str(client_id)),
        scope=html.escape(str(scope)),
        hidden=hidden,
        password_field=pf,
        error=html.escape(str(error)),
    )


def _oauth_redirect_origin(redirect_uri: str) -> str:
    """Return a CSP host-source for an already validated OAuth callback."""
    try:
        parsed = urlsplit(redirect_uri)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password):
            return ""
        port = parsed.port
    except ValueError:
        return ""
    hostname = parsed.hostname
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return (
        f"{parsed.scheme}://{hostname}"
        f"{f':{port}' if port is not None else ''}"
    )


def _oauth_consent_response(content: str, redirect_uri: str,
                            status_code: int = 200):
    from fastapi.responses import HTMLResponse
    callback_origin = _oauth_redirect_origin(redirect_uri)
    form_action = f"'self' {callback_origin}" if callback_origin else "'self'"
    return HTMLResponse(content, status_code=status_code, headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; "
            f"form-action {form_action}; "
            "base-uri 'none'; frame-ancestors 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    })


def _hidden(**kv):
    import html as _h
    return "".join(f'<input type=hidden name={k} value="{_h.escape(str(v))}">' for k, v in kv.items())


@app.get("/oauth/authorize")
async def oauth_authorize_get(request: Request, client_id: str = "", redirect_uri: str = "",
                              code_challenge: str = "", code_challenge_method: str = "S256",
                              scope: str = "codex", state: str = "",
                              resource: str = ""):
    """GET:显示授权同意页(用户确认后 POST 签发)。"""
    resource = resource or auth.resource
    if len(state) > 2048:
        raise HTTPException(400, "invalid_state")
    _validate_authorization(client_id, redirect_uri, code_challenge,
                            code_challenge_method, scope, resource)
    hidden = _hidden(client_id=client_id, redirect_uri=redirect_uri, code_challenge=code_challenge,
                     code_challenge_method=code_challenge_method, scope=scope, state=state,
                     resource=resource)
    return _oauth_consent_response(
        _consent_page(client_id, scope, hidden, bool(settings.oauth_password)),
        redirect_uri,
    )


@app.post("/oauth/authorize")
async def oauth_authorize_post(request: Request):
    """POST:校验密码(若配置)+ 同意/拒绝 → 签发 code 并 302。"""
    from urllib.parse import parse_qs
    if "application/x-www-form-urlencoded" not in request.headers.get("content-type", ""):
        raise HTTPException(415, "authorization form must be urlencoded")
    try:
        form = {key: values[0] for key, values in parse_qs(
            (await _read_body_limited(request, 32 * 1024)).decode("utf-8"),
            keep_blank_values=True,
        ).items()}
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "invalid authorization form") from exc
    client_id = str(form.get("client_id", ""))
    redirect_uri = str(form.get("redirect_uri", ""))
    code_challenge = str(form.get("code_challenge", ""))
    method = str(form.get("code_challenge_method", "S256"))
    scope = str(form.get("scope", "codex"))
    state = str(form.get("state", ""))
    if len(state) > 2048:
        raise HTTPException(400, "invalid_state")
    resource = str(form.get("resource", "")) or auth.resource
    _validate_authorization(client_id, redirect_uri, code_challenge, method, scope, resource)
    if form.get("deny"):
        return RedirectResponse(_oauth_redirect(redirect_uri, error="access_denied", state=state), status_code=302)
    if (settings.oauth_password and not hmac.compare_digest(
            str(form.get("password", "")), settings.oauth_password)):
        hidden = _hidden(client_id=client_id, redirect_uri=redirect_uri, code_challenge=code_challenge,
                         code_challenge_method=method, scope=scope, state=state, resource=resource)
        return _oauth_consent_response(
            _consent_page(client_id, scope, hidden, True, "密码错误,请重试"),
            redirect_uri,
            status_code=401,
        )
    code = auth.store.issue_code(client_id, redirect_uri, code_challenge, method,
                                 "user", scope, resource)
    return RedirectResponse(_oauth_redirect(redirect_uri, code=code, state=state), status_code=302)


@app.post("/oauth/token")
async def oauth_token(request: Request):
    _require_oauth_mode()
    body = await _read_body_limited(request, 32 * 1024)
    if "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
        from urllib.parse import parse_qs
        try:
            form = {k: v[0] for k, v in parse_qs(
                body.decode("utf-8"), keep_blank_values=True).items()}
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "invalid token request") from exc
    else:
        try:
            form = json.loads(body or b"{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "invalid token request") from exc
    if not isinstance(form, dict):
        raise HTTPException(400, "invalid token request")
    grant_type = form.get("grant_type")
    if grant_type == "authorization_code":
        rec = auth.store.redeem_code(
            form.get("code", ""), form.get("client_id", ""),
            form.get("redirect_uri", ""), form.get("code_verifier", ""))
        if not rec:
            raise HTTPException(400, "invalid_grant")
        if form.get("resource") != rec.get("resource"):
            raise HTTPException(400, "invalid_target")
        client = auth.store.get_client(rec.get("client_id", ""))
        if (not client or "authorization_code" not in
                (client.get("grant_types") or ["authorization_code"])):
            raise HTTPException(400, "invalid_client")
        return _oauth_token_response(
            rec["user_id"], rec.get("scope", "codex").split(),
            rec.get("resource", ""), rec.get("client_id", ""), client)
    if grant_type == "refresh_token":
        principal = auth.signer.verify_refresh(
            str(form.get("refresh_token") or ""), auth.accepts_resource)
        client_id = str(form.get("client_id") or "")
        client = auth.store.get_client(client_id)
        if (not principal or not client or principal.client_id != client_id
                or "refresh_token" not in (client.get("grant_types") or [])):
            raise HTTPException(400, "invalid_grant")
        resource = str(form.get("resource") or principal.audience)
        if resource != principal.audience or not auth.accepts_resource(resource):
            raise HTTPException(400, "invalid_target")
        scopes = principal.scopes
        if "scope" in form:
            requested_scopes = str(form.get("scope") or "").split()
            if (not requested_scopes
                    or not set(requested_scopes).issubset(set(principal.scopes))):
                raise HTTPException(400, "invalid_scope")
            scopes = requested_scopes
        return _oauth_token_response(
            principal.user_id, scopes, resource, client_id, client)
    raise HTTPException(400, "unsupported_grant_type")


def _oauth_token_response(user_id: str, scopes: list[str], resource: str,
                          client_id: str, client: dict) -> JSONResponse:
    payload = {
        "access_token": auth.signer.issue(
            user_id, scopes, audience=resource, client_id=client_id),
        "token_type": "Bearer",
        "expires_in": settings.oauth_token_ttl,
        "scope": " ".join(scopes),
    }
    if "refresh_token" in (client.get("grant_types") or []):
        payload["refresh_token"] = auth.signer.issue(
            user_id, scopes, audience=resource, client_id=client_id,
            token_use="refresh", ttl=settings.oauth_refresh_token_ttl)
    return JSONResponse(payload, headers={
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    })


def _require_oauth_mode() -> None:
    if settings.mcp_auth_mode not in ("oauth", "both"):
        raise HTTPException(404, "oauth is not enabled")


def _validate_authorization(client_id: str, redirect_uri: str, challenge: str,
                            method: str, scope: str, resource: str) -> None:
    _require_oauth_mode()
    if (len(client_id) > 128 or len(redirect_uri) > 2048
            or len(resource) > 2048):
        raise HTTPException(400, "invalid_request")
    client = auth.store.get_client(client_id)
    if not client or redirect_uri not in client.get("redirect_uris", []):
        raise HTTPException(400, "invalid_client_or_redirect_uri")
    if (settings.oauth_callback_protection and
            not is_chatgpt_connector_callback(redirect_uri)):
        raise HTTPException(400, "redirect_uri_not_allowed")
    if method != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
        raise HTTPException(400, "invalid_code_challenge")
    scopes = set(scope.split())
    if not scopes or not scopes.issubset({"codex"}):
        raise HTTPException(400, "invalid_scope")
    if not auth.accepts_resource(resource):
        raise HTTPException(400, "invalid_target")


def _oauth_redirect(redirect_uri: str, **params: str) -> str:
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parsed = urlsplit(redirect_uri)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((k, v) for k, v in params.items() if v != "")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                       urlencode(query), parsed.fragment))


# ---------- 审批与实时事件 ----------
@app.get("/api/approvals")
async def approvals(
        conversationId: str = "",
        p: Principal = Depends(principal),
):
    return {"pending": approval.list_pending(conversationId or None)}


@app.post("/api/approvals/{request_id}/decision")
async def approval_decision(
        request_id: str, request: Request,
        p: Principal = Depends(principal),
):
    body = await request.json()
    ok = await approval.resolve(
        request_id,
        {
            "action": body.get("decision") or "decline",
            "answers": body.get("answers"),
            "content": body.get("content"),
            "permissions": body.get("permissions"),
            "scope": body.get("scope"),
        },
        conversation_id=body.get("conversationId"),
        expected_version=body.get("expectedVersion"),
        decided_by=p.user_id,
    )
    if not ok:
        raise HTTPException(
            409, "approval is missing, expired, changed, or already resolved"
        )
    return {"resolved": True, "requestId": request_id}


@app.get("/api/events")
async def event_stream(
        request: Request, conversationId: str,
        lastEventId: int = 0,
        p: Principal = Depends(principal),
):
    """Authenticated, replayable one-way WebChat event channel."""
    if not conversationId:
        raise HTTPException(422, "conversationId is required")
    header_id = request.headers.get("last-event-id", "")
    try:
        after_id = max(lastEventId, int(header_id or 0))
    except ValueError:
        after_id = max(lastEventId, 0)

    async def stream():
        nonlocal after_id
        capabilities = {
            "eventId": after_id,
            "conversationId": conversationId,
            "capabilities": {
                **orch.router.capabilities(),
                "appServerMode": settings.codex_app_mode,
            },
        }
        yield (
            "event: runtime.capabilities\n"
            f"data: {json.dumps(capabilities, ensure_ascii=False)}\n\n"
        )
        while not await request.is_disconnected():
            delivered = False
            async for item in events.subscribe(conversationId, after_id):
                delivered = True
                after_id = item.event_id
                yield item.as_sse()
                if await request.is_disconnected():
                    return
            if not delivered:
                yield ": heartbeat\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------- codex app-server 管理 ----------
@app.get("/api/appserver/status")
async def appserver_status(p: Principal = Depends(principal)):
    return {
        **appserver.status(),
        "executionCapabilities": {
            **orch.router.capabilities(),
            "appServerMode": settings.codex_app_mode,
        },
    }


@app.post("/api/appserver/restart")
async def appserver_restart(p: Principal = Depends(principal)):
    try:
        return await appserver.restart()
    except Exception as e:
        raise HTTPException(500, f"restart failed: {e}")


@app.post("/api/appserver/stop")
async def appserver_stop(p: Principal = Depends(principal)):
    await appserver.stop()
    return {"running": False}


@app.get("/api/native/status")
async def native_status(p: Principal = Depends(principal)):
    return native.status()


@app.post("/api/native/codex/install")
async def native_codex_install(request: Request, p: Principal = Depends(principal)):
    body = await request.json()
    try:
        result = await asyncio.to_thread(
            native.install_codex,
            repository=(body.get("repository") or settings.codex_release_repo),
            source_url=body.get("url", ""),
            archive_path=body.get("archivePath", ""),
            github_token=body.get("githubToken", ""),
        )
        settings_store.set("codex_command", result["codexCommand"])
        appserver.settings = replace(
            appserver.settings, codex_command=result["codexCommand"])
        orch.settings = appserver.settings
        orch.router.settings = appserver.settings
        orch.router.execpolicy.settings = appserver.settings
        return result
    except NativeRuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/native/tunnel-client/install")
async def native_tunnel_install(request: Request, p: Principal = Depends(principal)):
    body = await request.json()
    try:
        result = await asyncio.to_thread(
            native.install_tunnel_client,
            body.get("release") or settings.tunnel_client_release,
        )
        settings_store.set("tunnel_client_command", result["tunnelCommand"])
        tunnels.settings = replace(
            tunnels.settings, tunnel_client_command=result["tunnelCommand"])
        return result
    except NativeRuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


# ---------- 概览(面板首页) ----------
@app.get("/api/overview")
async def overview(p: Principal = Depends(principal)):
    contexts = execution_registry.list()
    return {
        "appserver": appserver.status(),
        "publicRoute": tunnels.status(_PUBLIC_ROUTE_INSTANCE),
        "chatgptTunnel": tunnels.status(_CHATGPT_MCP_INSTANCE),
        "executionContexts": {
            "total": len(contexts),
            "active": sum(1 for context in contexts if context.status == "active"),
            "archived": sum(
                1 for context in contexts if context.status == "archived"
            ),
        },
        "pendingApprovals": len(approval.list_pending()),
        "executionCapabilities": orch.router.capabilities(),
        "auth": {"web": "token", "mcp": settings.mcp_auth_mode},
    }


# ---------- 运行时设置 ----------
@app.get("/api/settings")
async def get_settings(p: Principal = Depends(principal)):
    return {"settings": _masked_settings(_effective_settings()), "defaultsEditable": True}


@app.get("/api/oauth/metadata-audit")
async def oauth_metadata_audit(p: Principal = Depends(principal)):
    return _oauth_metadata_audit()


@app.get("/api/mcp-tools")
async def get_mcp_tools(p: Principal = Depends(principal)):
    """Enumerate downstream MCP tools and their Gateway exposure policy.

    The inventory itself is global to the App Server (not per-conversation), so
    this uses a throwaway identity context purely to satisfy the orchestrator's
    active-context requirement; the returned tool set and policies are global.
    """
    try:
        meta = {"conversationId": "__panel_mcp_inventory__"}
        data = await orch.list_mcp_tools("panel", meta)
    except ExecutionError:
        # No active workspace yet; fall back to a thread-free inventory without
        # the per-conversation wrapper so the panel can still manage policy.
        data = {"conversationId": "", "servers": await _panel_mcp_inventory()}
    data["policies"] = orch.mcp_tool_policies()
    data["policy"] = "registered-gateway-tools-only"
    data["default"] = "deny"
    data["mcpForwarding"] = True
    return data


async def _panel_mcp_inventory() -> list:
    try:
        response = await appserver.mcp_server_status_list()
    except Exception:
        return []
    servers = (response or {}).get("data") or []
    out = []
    for server in servers:
        name = str(server.get("name") or "")
        raw_tools = server.get("tools") or {}
        tool_items = list(raw_tools.values()) if isinstance(
            raw_tools, dict) else list(raw_tools or [])
        tools = []
        for tool in tool_items:
            tool_name = str(tool.get("name") or "")
            annotations = tool.get("annotations") or {}
            tools.append({
                "name": tool_name,
                "description": str(tool.get("description") or ""),
                "readOnly": bool(annotations.get("readOnlyHint")),
                "policy": (orch.mcp_tool_policies().get(f"{name}/{tool_name}", "deny")),
            })
        tools.sort(key=lambda item: item["name"])
        out.append({"name": name, "tools": tools})
    out.sort(key=lambda item: item["name"])
    return out


@app.post("/api/mcp-tools/policy")
async def set_mcp_tool_policy(request: Request, p: Principal = Depends(principal)):
    body = await _read_json_body_limited(request, 64 * 1024)
    policies = body.get("policies")
    try:
        current = orch.set_mcp_tool_policy(policies if isinstance(policies, dict) else {})
    except ExecutionError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"policies": current}


@app.post("/api/settings")
async def set_settings(request: Request, p: Principal = Depends(principal)):
    body = await _read_json_body_limited(request, 128 * 1024)
    body = {k: v for k, v in body.items() if v != "********"}
    # Informational value returned by GET; never persist it as configuration.
    body.pop("runtime_public_url", None)
    if ("mcp_auth_mode" in body and
            body["mcp_auth_mode"] not in {"token", "oauth", "both", "noauth"}):
        raise HTTPException(422, "mcp_auth_mode must be token, oauth, both, or noauth")
    if ("codex_app_mode" in body and
            body["codex_app_mode"] not in {"internal", "external"}):
        raise HTTPException(422, "codex_app_mode must be internal or external")
    if ("public_route_kind" in body and
            body["public_route_kind"] not in _PUBLIC_ROUTE_KINDS | {""}):
        raise HTTPException(
            422, "public_route_kind must be direct, cloudflared-try, or cloudflared-named")
    enum_settings = {
        "approval_policy": {"untrusted", "on-request", "never"},
        "sandbox": {"read-only", "workspace-write", "danger-full-access"},
        "work_mode": {"plan", "agent"},
    }
    for key, allowed in enum_settings.items():
        if key in body and body[key] not in allowed:
            raise HTTPException(
                422, f"{key} must be one of: {', '.join(sorted(allowed))}")
    bool_settings = {
        "oauth_callback_protection", "codex_auto_restart",
        "tunnel_auto_restart", "chatgpt_tunnel_enabled",
    }
    invalid_bools = sorted(
        key for key in bool_settings
        if key in body and not isinstance(body[key], bool))
    if invalid_bools:
        raise HTTPException(
            422, f"settings must be boolean: {', '.join(invalid_bools)}")
    if "codex_ws_port" in body:
        value = body["codex_ws_port"]
        if isinstance(value, bool):
            raise HTTPException(422, "codex_ws_port must be an integer from 1 to 65535")
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                422, "codex_ws_port must be an integer from 1 to 65535") from exc
        if not 1 <= value <= 65535:
            raise HTTPException(422, "codex_ws_port must be an integer from 1 to 65535")
        body["codex_ws_port"] = value
    if "approval_timeout_ms" in body:
        value = body["approval_timeout_ms"]
        if isinstance(value, bool):
            raise HTTPException(
                422, "approval_timeout_ms must be an integer from 1000 to 3600000"
            )
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                422, "approval_timeout_ms must be an integer from 1000 to 3600000"
            ) from exc
        if not 1_000 <= value <= 3_600_000:
            raise HTTPException(
                422, "approval_timeout_ms must be an integer from 1000 to 3600000"
            )
        body["approval_timeout_ms"] = value
    # Never migrate the old global tunnel selector back to ChatGPT Tunnel.
    body.pop("tunnel_kind", None)
    # Tunnel runtime keys belong in environment/secret storage, not sqlite.
    body.pop("chatgpt_api_key", None)
    body.pop("cloudflared_token", None)
    updated = settings_store.update(body)
    if "chatgpt_tunnel_id" in body:
        auth.set_chatgpt_tunnel_id(str(updated.get("chatgpt_tunnel_id") or ""))
    if any(key in body for key in {
            "codex_command", "codex_app_mode", "codex_external_ws_url",
            "codex_external_ws_key", "codex_release_repo", "codex_download_url",
            "tunnel_client_command", "tunnel_client_release", "tunnel_auto_restart",
            "chatgpt_tunnel_enabled", "chatgpt_tunnel_id", "public_route_kind"}):
        runtime = replace(
            appserver.settings,
            codex_command=updated.get("codex_command") or settings.codex_command,
            codex_app_mode=updated.get("codex_app_mode") or settings.codex_app_mode,
            codex_external_ws_url=(updated.get("codex_external_ws_url") or
                                   settings.codex_external_ws_url),
            codex_external_ws_key=(updated.get("codex_external_ws_key") or
                                   settings.codex_external_ws_key),
            codex_release_repo=(updated.get("codex_release_repo") or
                                settings.codex_release_repo),
            codex_download_url=(updated.get("codex_download_url") or
                                settings.codex_download_url),
            tunnel_client_command=(updated.get("tunnel_client_command") or
                                   settings.tunnel_client_command),
            tunnel_client_release=(updated.get("tunnel_client_release") or
                                   settings.tunnel_client_release),
            tunnel_auto_restart=bool(updated.get("tunnel_auto_restart", True)),
            chatgpt_tunnel_enabled=bool(updated.get(
                "chatgpt_tunnel_enabled", False)),
            chatgpt_tunnel_id=(updated.get("chatgpt_tunnel_id") or
                               settings.chatgpt_tunnel_id),
            public_route_kind=(updated.get("public_route_kind") or ""),
            tunnel_kind=(updated.get("public_route_kind") or ""),
        )
        appserver.settings = runtime
        orch.settings = runtime
        orch.router.settings = runtime
        orch.router.execpolicy.settings = runtime
        tunnels.settings = runtime
    if "codex_ws_port" in body:
        appserver.port = int(updated["codex_ws_port"])
        appserver.configured_port = appserver.port
    if "codex_auto_restart" in body:
        appserver.auto_restart = bool(updated["codex_auto_restart"])
    if "approval_timeout_ms" in body:
        approval.default_timeout = int(updated["approval_timeout_ms"]) / 1000.0
    restart_required = sorted(set(body) & {
        "web_access_token", "mcp_auth_mode", "mcp_access_token",
        "oauth_password", "oauth_callback_protection", "public_url",
    })
    appserver_restart_required = sorted(set(body) & {
        "codex_command", "codex_app_mode", "codex_external_ws_url",
        "codex_external_ws_key", "codex_ws_port",
    })
    return {"settings": _masked_settings(_effective_settings()),
            "restartRequired": restart_required,
            "appserverRestartRequired": appserver_restart_required}


def _masked_settings(values: dict) -> dict:
    values = dict(values)
    for key in ("web_access_token", "mcp_access_token", "oauth_password",
                "cloudflared_token", "chatgpt_api_key", "codex_external_ws_key",
                "codex_internal_ws_key"):
        values[key] = "********" if values.get(key) else ""
    return values


def _effective_settings() -> dict:
    """Return what the running Gateway actually uses, not bare UI defaults.

    Without this merge, opening the settings page and pressing Save could
    persist a DEFAULTS value over an environment-provided port, command, or
    auth setting that was never visible in the form.
    """
    values = settings_store.all()
    fallbacks = {
        "public_route_kind": settings.public_route_kind,
        "tunnel_kind": settings.public_route_kind,
        "chatgpt_tunnel_enabled": settings.chatgpt_tunnel_enabled,
        "chatgpt_tunnel_id": settings.chatgpt_tunnel_id,
        "tunnel_client_command": settings.tunnel_client_command,
        "tunnel_client_release": settings.tunnel_client_release,
        "tunnel_auto_restart": settings.tunnel_auto_restart,
        "web_access_token": settings.web_access_token,
        "mcp_auth_mode": settings.mcp_auth_mode,
        "mcp_access_token": settings.mcp_access_token,
        "oauth_password": settings.oauth_password,
        "oauth_callback_protection": settings.oauth_callback_protection,
        "public_url": settings.public_url,
        "codex_app_mode": settings.codex_app_mode,
        "codex_command": settings.codex_command,
        "codex_external_ws_url": settings.codex_external_ws_url,
        "codex_external_ws_key": settings.codex_external_ws_key,
        "codex_internal_ws_key": settings.codex_internal_ws_key,
        "codex_release_repo": settings.codex_release_repo,
        "codex_download_url": settings.codex_download_url,
        "codex_ws_port": settings.codex_ws_port,
        "approval_timeout_ms": settings.approval_timeout_ms,
    }
    for key, fallback in fallbacks.items():
        override = settings_store.get_override(key)
        values[key] = fallback if override is None or override == "" else override
    values["runtime_public_url"] = auth.public_url
    return values


# ---------- WebChat execution contexts ----------
@app.get("/api/execution-contexts")
async def execution_contexts(
        userId: str = "", status: str = "",
        p: Principal = Depends(principal),
):
    return {"contexts": [
        ExecutionRegistry.to_dict(context)
        for context in execution_registry.list(
            user_id=userId or None, status=status or None)
    ]}


@app.post("/api/execution-contexts/{context_id}/status")
async def execution_context_status(
        context_id: str, request: Request,
        p: Principal = Depends(principal),
):
    requested_status = (await request.json()).get("status", "active")
    if requested_status not in {"active", "archived", "closed"}:
        raise HTTPException(422, "status must be active, archived, or closed")
    context = execution_registry.set_status(context_id, requested_status)
    if context and requested_status != "active":
        approval.cancel_pending(
            "conversation_closed", context.conversation_id
        )
    return {
        "context": ExecutionRegistry.to_dict(context) if context else None
    }


@app.get("/api/execution-contexts/{context_id}")
async def execution_context_detail(
        context_id: str, p: Principal = Depends(principal),
):
    context = execution_registry.get(context_id)
    if not context:
        raise HTTPException(404, "execution context not found")
    return {"context": ExecutionRegistry.to_dict(context)}


@app.delete("/api/execution-contexts/{context_id}")
async def execution_context_delete(
        context_id: str, p: Principal = Depends(principal),
):
    context = execution_registry.get(context_id)
    if context:
        approval.cancel_pending(
            "conversation_deleted", context.conversation_id
        )
    execution_registry.delete(context_id)
    return {"deleted": context_id}


# ---------- 全局公网入口（仅 direct / cloudflared） ----------
@app.get("/api/public-route/status")
@app.get("/api/tunnel/status")
async def public_route_status(p: Principal = Depends(principal)):
    return tunnels.status(_PUBLIC_ROUTE_INSTANCE)


@app.post("/api/public-route/start")
@app.post("/api/tunnel/start")
async def public_route_start(request: Request, p: Principal = Depends(principal)):
    body = await request.json()
    kind = body.get("kind", "direct")
    if kind not in {"direct", "cloudflared"}:
        raise HTTPException(
            422, "global public route only supports direct or cloudflared")
    mode = body.get("mode", "try") if kind == "cloudflared" else ""
    if kind == "cloudflared" and mode not in {"try", "named"}:
        raise HTTPException(422, "cloudflared mode must be try or named")
    stored_kind = f"cloudflared-{mode}" if kind == "cloudflared" else "direct"
    settings_store.set("public_route_kind", stored_kind)
    # A previous TryCloudflare run may have supplied an ephemeral runtime URL.
    # New direct/named starts must begin from the configured startup URL.
    tunnels.settings = replace(tunnels.settings, public_url=settings.public_url)
    options = {"instance_id": _PUBLIC_ROUTE_INSTANCE}
    if kind == "cloudflared":
        options.update(mode=mode, token=body.get("token", ""))
    return await tunnels.start(kind, **options)


@app.post("/api/public-route/stop")
@app.post("/api/tunnel/stop")
async def public_route_stop(p: Principal = Depends(principal)):
    await tunnels.stop(_PUBLIC_ROUTE_INSTANCE)
    _replace_runtime_public_url(settings.public_url, https_required=False)
    return {"ok": True}


# ---------- ChatGPT Secure MCP Tunnel（不参与全局公网路由） ----------
@app.get("/api/chatgpt-tunnel/status")
async def chatgpt_tunnel_status(p: Principal = Depends(principal)):
    return tunnels.status(_CHATGPT_MCP_INSTANCE)


@app.post("/api/chatgpt-tunnel/start")
async def chatgpt_tunnel_start(request: Request,
                               p: Principal = Depends(principal)):
    body = await request.json()
    tunnel_id = str(body.get("tunnel_id") or settings.chatgpt_tunnel_id)
    if body.get("tunnel_id"):
        settings_store.set("chatgpt_tunnel_id", tunnel_id)
    auth.set_chatgpt_tunnel_id(tunnel_id)
    settings_store.set("chatgpt_tunnel_enabled", True)
    # Runtime API key is process-only. CONTROL_PLANE_API_KEY is required for
    # restart-safe autostart and is deliberately never written to sqlite.
    return await tunnels.start(
        "chatgpt", instance_id=_CHATGPT_MCP_INSTANCE,
        tunnel_id=tunnel_id, api_key=body.get("api_key", ""),
        client_bin=body.get("client_bin", ""))


@app.post("/api/chatgpt-tunnel/stop")
async def chatgpt_tunnel_stop(p: Principal = Depends(principal)):
    settings_store.set("chatgpt_tunnel_enabled", False)
    await tunnels.stop(_CHATGPT_MCP_INSTANCE)
    return {"ok": True}


def main() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host=settings.host, port=settings.port,
                reload=False, log_level="info")


if __name__ == "__main__":
    main()
