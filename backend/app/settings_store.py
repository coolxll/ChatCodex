"""运行时设置:存 kv_config,影响后续执行上下文与隧道/行为。

与环境变量(Settings,只读)区分:这些是面板运行时可调的。
读取顺序:运行时设置 > 环境默认。
"""
from __future__ import annotations

import json
from typing import Any

from .db import Database

# 可调项及默认值
DEFAULTS: dict[str, Any] = {
    "approval_policy": "on-request",    # untrusted|on-request|never (granular is experimental)
    "sandbox": "workspace-write",       # read-only|workspace-write|danger-full-access
    "work_mode": "agent",               # plan|agent; safety is configured separately
    "approval_timeout_ms": 300_000,      # approval wait, separate from command timeout
    "public_route_kind": "",            # ""|direct|cloudflared-try|cloudflared-named
    "tunnel_kind": "",                  # 旧版本迁移键；不再接受 chatgpt 作为全局路由
    "chatgpt_tunnel_enabled": False,     # 独立 MCP Secure Tunnel
    "chatgpt_tunnel_id": "",
    "tunnel_client_command": "tunnel-client",
    "tunnel_client_release": "v0.0.11-dev",
    "tunnel_auto_restart": True,
    # ---- 认证:Web 与 MCP 是两个独立安全边界 ----
    "web_access_token": "",             # 管理页面和 /api/*
    "mcp_auth_mode": "token",           # token|oauth|both|noauth
    "mcp_access_token": "",             # /mcp 静态 Bearer(token/both)
    "oauth_password": "",               # oauth 同意页密码
    "oauth_callback_protection": False,   # 仅允许 ChatGPT connector 回调
    "public_url": "",                   # 公网地址(OAuth issuer/CSP;空=本机)
    # ---- codex app-server(原 CHATCODEX_CODEX_*) ----
    "codex_command": "",                # codex 二进制(空=自动解析)
    "codex_app_mode": "internal",        # internal|external
    "codex_external_ws_url": "",
    "codex_external_ws_key": "",
    "codex_internal_ws_key": "",
    "codex_release_repo": "openai/codex",
    "codex_download_url": "",
    "codex_ws_port": 8765,              # app-server ws 端口
    "codex_auto_restart": True,         # 看护自动重启
}

def _migrate_value(key: str, value: Any) -> Any:
    """Translate obsolete defaults without mutating unrelated user settings."""
    if (key == "codex_release_repo" and isinstance(value, str) and
            value.casefold() == "aeroideslab/codexext"):
        return "openai/codex"
    if key == "work_mode" and value == "agent-full-access":
        return "agent"
    return value


class SettingsStore:
    def __init__(self, db: Database):
        self.db = db

    def all(self) -> dict[str, Any]:
        out = dict(DEFAULTS)
        with self.db.conn() as c:
            rows = c.execute("SELECT key,value FROM kv_config WHERE key LIKE 'set:%'").fetchall()
        for r in rows:
            key = r["key"][4:]
            if key not in DEFAULTS:
                continue
            try:
                out[key] = _migrate_value(key, json.loads(r["value"]))
            except Exception:
                pass
        return out

    def get(self, key: str) -> Any:
        return self.all().get(key, DEFAULTS.get(key))

    def get_override(self, key: str) -> Any:
        """Return only an explicitly persisted value, not a UI default."""
        with self.db.conn() as c:
            row = c.execute("SELECT value FROM kv_config WHERE key=?",
                            (f"set:{key}",)).fetchone()
        if not row:
            return None
        try:
            return _migrate_value(key, json.loads(row["value"]))
        except Exception:
            return None

    def set(self, key: str, value: Any) -> None:
        with self.db.conn() as c:
            c.execute("INSERT OR REPLACE INTO kv_config(key,value) VALUES(?,?)",
                      (f"set:{key}", json.dumps(value, ensure_ascii=False)))

    def update(self, kv: dict[str, Any]) -> dict[str, Any]:
        for k, v in kv.items():
            if k in DEFAULTS:
                self.set(k, v)
        return self.all()
