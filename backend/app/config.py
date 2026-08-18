"""Gateway 配置:环境变量驱动,sqlite 默认、可切 PostgreSQL。"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _default_codex_command() -> str:
    """Resolve an official Codex runtime without depending on a source fork."""
    workspace = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    name = "codex.exe" if os.name == "nt" else "codex"
    base = os.path.join(workspace, "native", "codex", "current")
    for native in (
        os.path.join(base, "bin", name),
        os.path.join(base, "codex", "bin", name),
        os.path.join(base, name),
    ):
        if os.path.isfile(native):
            return native
    return shutil.which("codex") or ""


def _native_dir() -> str:
    workspace = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    return _env("CHATCODEX_NATIVE_DIR", os.path.join(workspace, "native"))


def _legacy_database_path() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "chatcodex.db"))


def _default_database_path() -> str:
    """Keep credentials and session state out of the source checkout."""
    if os.name == "nt":
        base = _env("LOCALAPPDATA", os.path.expanduser("~"))
        return os.path.join(base, "ChatCodex", "chatcodex.db")
    base = _env("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state"))
    return os.path.join(base, "chatcodex", "chatcodex.db")


@dataclass(frozen=True)
class Settings:
    # HTTP 监听(本机回环;隧道把公网流量打到这里)
    host: str = _env("CHATCODEX_HOST", "127.0.0.1")
    port: int = int(_env("CHATCODEX_PORT", "18473"))

    # 数据库:sqlite:///path 或 postgresql://user:pass@host/db
    database_url: str = (
        _env("CHATCODEX_DATABASE_URL") or "sqlite:///" + _default_database_path()
    )

    # Codex App Server: internal 由 Gateway 守护；external 只连接已有 WebSocket。
    codex_app_mode: str = _env("CHATCODEX_CODEX_APP_MODE", "internal")
    codex_command: str = _env("CHATCODEX_CODEX_COMMAND", _default_codex_command())
    codex_external_ws_url: str = _env("CHATCODEX_CODEX_EXTERNAL_WS_URL", "")
    codex_external_ws_key: str = _env("CHATCODEX_CODEX_EXTERNAL_WS_KEY", "")
    codex_internal_ws_key: str = _env("CHATCODEX_CODEX_INTERNAL_WS_KEY", "")
    codex_ws_port: int = int(_env("CHATCODEX_CODEX_WS_PORT", "8765"))
    approval_timeout_ms: int = int(
        _env("CHATCODEX_APPROVAL_TIMEOUT_MS", "300000")
    )
    codex_release_repo: str = _env(
        "CHATCODEX_CODEX_RELEASE_REPO", "openai/codex")
    codex_download_url: str = _env("CHATCODEX_CODEX_DOWNLOAD_URL", "")
    native_dir: str = _native_dir()

    # Web 管理面板/API 与 MCP 使用独立凭据。旧 CHATCODEX_AUTH_* 仅作迁移兜底。
    web_access_token: str = _env(
        "CHATCODEX_WEB_ACCESS_TOKEN", _env("CHATCODEX_AUTH_TOKEN", ""))
    # token = 静态 Bearer; oauth = OAuth 2.1; both = 两者都接受; noauth 仅 loopback
    mcp_auth_mode: str = _env(
        "CHATCODEX_MCP_AUTH_MODE",
        {"bearer": "token"}.get(_env("CHATCODEX_AUTH_MODE", "token"),
                                  _env("CHATCODEX_AUTH_MODE", "token")),
    )
    mcp_access_token: str = _env("CHATCODEX_MCP_ACCESS_TOKEN", "")
    oauth_token_secret: str = _env("CHATCODEX_OAUTH_TOKEN_SECRET", "dev-secret-change-me")
    oauth_token_ttl: int = int(_env("CHATCODEX_OAUTH_TOKEN_TTL", "3600"))
    oauth_refresh_token_ttl: int = int(
        _env("CHATCODEX_OAUTH_REFRESH_TOKEN_TTL", "2592000"))
    # OAuth 同意页登录密码(oauth 模式必填;空=不校验,仅开发)
    oauth_password: str = _env("CHATCODEX_OAUTH_PASSWORD", "")
    oauth_callback_protection: bool = _env(
        "CHATCODEX_OAUTH_CALLBACK_PROTECTION", "0").lower() in {"1", "true", "yes", "on"}
    # Gateway 的全局公网 HTTPS 根地址，用于内置 OAuth issuer / widget CSP。
    # Secure MCP Tunnel 是独立的 MCP 传输，不改变这个全局地址。
    public_url: str = _env("CHATCODEX_PUBLIC_URL", "http://127.0.0.1:18473")

    # 前端构建产物目录(widget 资源);开发期也可内联
    frontend_dist: str = _env(
        "CHATCODEX_FRONTEND_DIST",
        os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist"),
    )

    # 全局公网入口只允许 direct / cloudflared。CHATCODEX_TUNNEL 仅作旧配置迁移。
    public_route_kind: str = _env(
        "CHATCODEX_PUBLIC_ROUTE", _env("CHATCODEX_TUNNEL", ""))
    tunnel_kind: str = public_route_kind  # 旧调用方兼容别名
    cloudflared_token: str = _env("CLOUDFLARED_TOKEN", "")       # named tunnel JWT
    # ChatGPT Tunnel 是独立 MCP 传输，不属于全局公网路由。
    chatgpt_tunnel_enabled: bool = _env(
        "CHATCODEX_CHATGPT_TUNNEL_ENABLED", "0").lower() in {
            "1", "true", "yes", "on"}
    chatgpt_tunnel_id: str = _env("CONTROL_PLANE_TUNNEL_ID", "")
    chatgpt_api_key: str = _env("CONTROL_PLANE_API_KEY", "")
    tunnel_client_command: str = _env("CHATCODEX_TUNNEL_CLIENT", "tunnel-client")
    tunnel_client_release: str = _env("CHATCODEX_TUNNEL_CLIENT_RELEASE", "v0.0.11-dev")
    tunnel_auto_restart: bool = _env(
        "CHATCODEX_TUNNEL_AUTO_RESTART", "1").lower() not in {"0", "false", "no", "off"}

    extra: dict = field(default_factory=dict)


def load_settings() -> Settings:
    return Settings()
