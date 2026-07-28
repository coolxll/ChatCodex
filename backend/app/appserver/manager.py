"""AppServerManager:codex app-server 的统一生命周期管理(Windows 可用)。

借鉴 codex app-server-daemon 的思想(单例/生命周期/版本上报),但 daemon 是
Unix-only,Windows 不支持进程管理。本管理器面向 Gateway 内嵌场景:

- 单例生命周期:start / stop / restart / status(像 daemon,但 Windows 可用)
- 健康探活:周期 ping(initialize 是幂等的只读握手),断线自动标记 dead
- 进程看护:监视子进程退出,可选自动重启(带退避)
- 传输统一:当前用 ws://(Windows 上 stdio 在 uvicorn 下不稳定),接口与
  IsolatedAppServer 一致(协程 + 回调注册 + status)
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import ipaddress
import os
import secrets
import shutil
import socket
import time
from typing import Optional
from urllib.parse import urlsplit

from ..config import Settings
from ..native import NativeRuntimeManager
from .isolated import IsolatedAppServer
from .ws_client import WsAppServerClient

# 健康探活间隔(秒)与连续失败阈值
PING_INTERVAL = 15.0
FAIL_THRESHOLD = 3
# 自动重启退避(秒)
RESTART_BACKOFF = [2, 5, 15, 60]


def _is_loopback_host(hostname: str) -> bool:
    """Return true only for names and addresses confined to this machine."""
    value = str(hostname or "").strip().rstrip(".").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


class AppServerManager:
    """单例管理一个 codex app-server(ws),带健康探活与自动看护。"""

    def __init__(self, settings: Settings, port: int = 8765, auto_restart: bool = True,
                 native: Optional[NativeRuntimeManager] = None):
        self.settings = settings
        self.native = native or NativeRuntimeManager(settings.native_dir)
        self.port = port
        self.configured_port = port
        self.auto_restart = auto_restart
        self._server: Optional[IsolatedAppServer] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._started = False
        self._healthy = False
        self._consecutive_failures = 0
        self._restart_attempts = 0
        self._last_error = ""
        self._started_at = 0.0
        self._instance_generation = 0
        self._on_server_request = None
        self._on_notification = None
        self._on_reset = None
        self._lifecycle_lock = asyncio.Lock()

    # ---- 回调注册(透传给底层,重启后重挂) ----
    def on_server_request(self, handler) -> None:
        self._on_server_request = handler
        if self._server:
            self._server.on_server_request(handler)

    def on_notification(self, handler) -> None:
        self._on_notification = handler
        if self._server:
            self._server.on_notification(handler)

    def on_reset(self, handler) -> None:
        """Run a synchronous hook before each fresh app-server instance."""
        self._on_reset = handler

    # ---- 生命周期 ----
    async def start(self) -> None:
        async with self._lifecycle_lock:
            await self._start_unlocked()

    async def _start_unlocked(self) -> None:
        if self._started:
            return
        try:
            await self._spawn()
        except Exception as exc:
            self._healthy = False
            self._last_error = f"start failed: {exc}"[:500]
            raise
        self._started = True
        self._started_at = time.time()
        self._watchdog_task = asyncio.create_task(self._watchdog())

    async def _spawn(self) -> None:
        if self._on_reset:
            self._on_reset()
        self._instance_generation += 1
        mode = self.settings.codex_app_mode
        token_file = ""
        if mode == "external":
            endpoint = self.settings.codex_external_ws_url.strip()
            parsed = urlsplit(endpoint)
            if (parsed.scheme not in {"ws", "wss"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.fragment):
                raise ValueError("external Codex App Server URL must use ws:// or wss://")
            if parsed.scheme == "ws" and not _is_loopback_host(parsed.hostname):
                raise ValueError(
                    "external Codex App Server must use wss:// outside loopback")
            client = WsAppServerClient(
                self.settings,
                endpoint=endpoint,
                bearer_token=self.settings.codex_external_ws_key,
                spawn=False,
            )
        elif mode == "internal":
            self.port = self._available_port(self.port)
            command = self._resolve_internal_command()
            if not command:
                result = await asyncio.to_thread(
                    self.native.install_codex,
                    repository=self.settings.codex_release_repo,
                    source_url=self.settings.codex_download_url,
                    github_token=os.environ.get("GITHUB_TOKEN", ""),
                )
                command = result.get("codexCommand", "")
            if not command:
                raise RuntimeError("no internal Codex runtime is available")
            internal_key = (self.settings.codex_internal_ws_key or
                            secrets.token_urlsafe(48))
            self.settings = replace(
                self.settings, codex_command=command,
                codex_internal_ws_key=internal_key)
            token_file = self.native.internal_token_file(internal_key)
            client = WsAppServerClient(
                self.settings,
                port=self.port,
                endpoint=f"ws://127.0.0.1:{self.port}",
                bearer_token=self.settings.codex_internal_ws_key,
                token_file=token_file,
                spawn=True,
            )
        else:
            raise ValueError("codex_app_mode must be internal or external")
        server = IsolatedAppServer(self.settings, client)
        self._server = server
        if self._on_server_request:
            server.on_server_request(self._on_server_request)
        if self._on_notification:
            server.on_notification(self._on_notification)
        try:
            await server.start()
        except BaseException:
            try:
                await server.close()
            finally:
                if self._server is server:
                    self._server = None
            raise
        finally:
            # Codex reads the capability token once at startup and stores only
            # its SHA-256 digest. Do not retain the bearer credential on disk.
            if token_file:
                try:
                    os.unlink(token_file)
                except OSError:
                    pass
        self._healthy = True
        self._consecutive_failures = 0
        self._last_error = ""

    def _resolve_internal_command(self) -> str:
        configured = self.settings.codex_command.strip()
        if configured:
            if os.path.isfile(configured) or shutil.which(configured):
                return configured
        native = self.native.codex_command()
        if native:
            return native
        # A normal official CLI installation on PATH is a valid internal runtime.
        return shutil.which("codex") or ""

    def codex_command_for_exec(self) -> str:
        """Executable name/path understood by the app-server host.

        Internal mode can use the exact executable that started the managed
        server. For an external WebSocket, command/exec runs on the remote host;
        official package installs add `codex` to that process' PATH.
        """
        if self.settings.codex_app_mode == "external":
            return "codex"
        if self._server:
            command = str(self._server.status().get("command") or "").strip()
            if command:
                return command
        return self._resolve_internal_command() or "codex"

    @staticmethod
    def _available_port(preferred: int) -> int:
        """Use the configured port when free, otherwise allocate a private one."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self) -> None:
        self._started = False
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        if self._server:
            if self._on_reset:
                self._on_reset()
                # Let cancelled approval handlers return while the old
                # connection's event loop can still send their response.
                await asyncio.sleep(0)
            await self._server.close()
            self._server = None
        self._healthy = False

    async def restart(self) -> dict:
        async with self._lifecycle_lock:
            await self._stop_unlocked()
            await self._start_unlocked()
            self._restart_attempts = 0
            return self.status()

    # ---- 健康探活 + 看护 ----
    async def _watchdog(self) -> None:
        while self._started:
            await asyncio.sleep(PING_INTERVAL)
            ok = await self._ping()
            if ok:
                if not self._healthy:
                    self._healthy = True
                self._consecutive_failures = 0
                continue
            self._consecutive_failures += 1
            self._healthy = False
            if self._consecutive_failures >= FAIL_THRESHOLD and self.auto_restart:
                await self._attempt_restart()

    async def _ping(self) -> bool:
        """轻量探活:model/list 是只读快速 RPC。失败标记不健康。"""
        if not self._server:
            return False
        try:
            await self._server.call("model/list", {}, timeout=8)
            return True
        except Exception as e:
            self._last_error = str(e)[:200]
            return False

    async def _attempt_restart(self) -> None:
        if self._restart_attempts >= len(RESTART_BACKOFF):
            self._last_error = "max restart attempts reached"
            return
        backoff = RESTART_BACKOFF[self._restart_attempts]
        self._restart_attempts += 1
        await asyncio.sleep(backoff)
        async with self._lifecycle_lock:
            if not self._started:
                return
            try:
                if self._server:
                    if self._on_reset:
                        self._on_reset()
                        await asyncio.sleep(0)
                    await self._server.close()
            except Exception:
                pass
            try:
                await self._spawn()
            except Exception as e:
                self._last_error = f"restart failed: {e}"[:200]
                self._healthy = False

    # ---- 状态 ----
    def status(self) -> dict:
        base = self._server.status() if self._server else {}
        return {
            **base,
            "healthy": self._healthy,
            "managed": True,
            "native": self.native.status(),
            "autoRestart": self.auto_restart,
            "consecutiveFailures": self._consecutive_failures,
            "restartAttempts": self._restart_attempts,
            "configuredPort": self.configured_port,
            "actualPort": self.port,
            "portFallback": self.port != self.configured_port,
            "lastError": self._last_error,
            "instanceId": f"appserver-{self._instance_generation}",
            "managerUptimeSec": int(time.time() - self._started_at) if self._started_at else 0,
        }

    # ---- 代理到底层 server(协程 + 属性) ----
    @property
    def proc(self):
        return self._server.proc if self._server else None

    @property
    def initialize_result(self) -> dict:
        return self._server.initialize_result if self._server else {}

    def __getattr__(self, name: str):
        if name.startswith("_") or name in ("status", "start", "stop", "restart",
                                             "on_server_request", "on_notification", "on_reset",
                                             "proc", "initialize_result",
                                             "codex_command_for_exec"):
            raise AttributeError(name)
        if self._server is None:
            raise RuntimeError("appserver not started")
        return getattr(self._server, name)
