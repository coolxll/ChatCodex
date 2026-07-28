"""widget 资源提供:ui://widget/*.html。

读取 frontend/dist 构建产物(真实 React 版)。未构建时返回占位提示页。
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from .config import Settings
from .tools import (
    WIDGET_APPROVAL,
    WIDGET_ASK,
    WIDGET_CHAT,
    WIDGET_DIFF,
    WIDGET_WORKSPACE_SETUP,
)

WIDGETS = {
    WIDGET_WORKSPACE_SETUP: (
        "workspace-setup.html", "WebChat 执行工作区配置"
    ),
    WIDGET_CHAT: ("chat.html", "WebChat 本地执行状态"),
    WIDGET_ASK: ("ask-user.html", "需要用户输入"),
    WIDGET_APPROVAL: ("approval.html", "确认 Codex 操作"),
    WIDGET_DIFF: ("diff.html", "Codex 文件改动"),
}


def widget_domain(public_url: str) -> str:
    """Return a valid dedicated HTTPS widget origin, or an empty value."""
    value = str(public_url or "").rstrip("/")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        return ""
    return value


def resource_meta(description: str, public_url: str) -> dict:
    """Build standard MCP Apps metadata plus ChatGPT compatibility aliases."""
    domain = widget_domain(public_url)
    # The widget calls back into the Gateway (resolve_approval, execution_status
    # polling).  Declare that origin in the CSP allowlists so ChatGPT treats the
    # widget as CSP-declared instead of showing the "CSP off" badge.  Without a
    # public HTTPS domain (plain loopback) there is nothing meaningful to
    # declare, so the lists stay empty.
    connect_domains = [domain] if domain else []
    ui = {
        "prefersBorder": False,
        "csp": {"connectDomains": connect_domains, "resourceDomains": []},
    }
    meta = {
        "ui": ui,
        "openai/widgetDescription": description,
        "openai/widgetPrefersBorder": False,
        "openai/widgetCSP": {
            "connect_domains": connect_domains, "resource_domains": [],
        },
    }
    if domain:
        ui["domain"] = domain
        meta["openai/widgetDomain"] = domain
    return meta


def list_resources(settings: Settings) -> list[dict]:
    return [{
        "uri": uri, "name": desc, "mimeType": "text/html;profile=mcp-app",
        "_meta": resource_meta(desc, settings.public_url),
    } for uri, (_fn, desc) in WIDGETS.items()]


def read_resource(settings: Settings, uri: str) -> dict:
    fn = WIDGETS.get(uri, ("", ""))[0]
    html = _load_dist(settings, fn) if fn else None
    if html is None:
        html = _placeholder(uri)
    return {"uri": uri, "mimeType": "text/html;profile=mcp-app", "text": html}


def _load_dist(settings: Settings, filename: str) -> str | None:
    path = os.path.join(settings.frontend_dist, filename)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return None


def _placeholder(uri: str) -> str:
    return (f"<!doctype html><meta charset=utf-8><body style='font-family:sans-serif;padding:24px'>"
            f"<h3>widget 未构建</h3><p>{uri}</p>"
            f"<p>运行 <code>cd frontend && npm install && npm run build</code> 后重启 Gateway。</p>")
