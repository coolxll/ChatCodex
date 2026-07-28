"""Shared JSON-RPC contracts for the WebSocket App Server client."""
from __future__ import annotations

from typing import Any, Awaitable, Callable


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


# 回调签名
ServerRequestHandler = Callable[[dict], Awaitable[dict]]  # 入参完整 request,返回 result(将被包成 response)
NotificationHandler = Callable[[str, Any], Awaitable[None]]  # (method, params)
