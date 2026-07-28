"""空闲 ephemeral Codex thread 作为 MCP 转发载体。

每个 ExecutionContext 懒建一个 ``ephemeral`` thread(绝不调 ``turn/start``),
仅作为 ``mcpServerStatus/list`` 与 ``mcpServer/tool/call`` 的宿主。它不启动
任何模型循环;模型推理只在官方 ``turn/start`` 触发,而本模块从不调用它。

生命周期要点(与 codex-rs 审计一致):
- ephemeral thread 只活在 app-server 内存,不落 rollout,不可 resume。
- 空闲(无订阅且非活跃)约 30 分钟会被卸载;app-server 重启即丢失。
- 因此本模块按需探活,发现 ``ThreadNotFound`` 即重建;不做无谓的空转保活,
  因为转发调用本身就会重置活跃计时。
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional


class McpCarrier:
    """Owns one ephemeral carrier thread per WebChat execution context."""

    def __init__(self, appserver: Any):
        self.appserver = appserver
        self._threads: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @staticmethod
    def _is_missing_thread(exc: Exception) -> bool:
        text = str(exc).lower()
        return "thread not found" in text or "thread_not_found" in text

    async def _spawn(self) -> str:
        result = await self.appserver.thread_start(ephemeral=True)
        thread_id = str((result or {}).get("thread", {}).get("id") or "")
        if not thread_id:
            raise RuntimeError("thread/start did not return a thread id")
        return thread_id

    async def thread_id(self, key: str) -> str:
        """Return a live carrier thread for ``key``, creating it on demand."""
        async with self._lock_for(key):
            existing = self._threads.get(key)
            if existing:
                try:
                    await self.appserver.mcp_server_status_list(
                        existing, detail="NameOnly", limit=1)
                    return existing
                except Exception as exc:
                    if not self._is_missing_thread(exc):
                        # A transient error (server still starting) should not
                        # throw away a probably-live thread.
                        return existing
                    self._threads.pop(key, None)
            fresh = await self._spawn()
            self._threads[key] = fresh
            return fresh

    async def drop(self, key: str) -> None:
        """Forget and best-effort close the carrier thread for ``key``."""
        async with self._lock_for(key):
            thread_id = self._threads.pop(key, None)
        if thread_id:
            try:
                await self.appserver.call(
                    "thread/archive", {"threadId": thread_id}, timeout=5)
            except Exception:
                pass

    async def drop_all(self) -> None:
        for key in list(self._threads):
            await self.drop(key)

    def tracked(self) -> dict[str, str]:
        return dict(self._threads)
