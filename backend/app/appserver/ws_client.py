"""WebSocket 传输的 codex app-server 客户端。

ws:// 是 codex 为网络/多客户端设计的原生传输,绕开 Windows 上
uvicorn + stdio 子进程管道的兼容问题。回环(127.0.0.1)免鉴权。

接口与传输无关,可互换。
"""
from __future__ import annotations

import asyncio
from collections import deque
import inspect
import json
import socket
import tempfile
import time
from typing import Any, Optional

import websockets

from ..config import Settings
from ..process_guard import attach_windows_kill_job, close_windows_kill_job
from .jsonrpc import ServerRequestHandler, NotificationHandler
from .resolve import CLIENT_NAME, CLIENT_VERSION, resolve_codex_executable
from .rpc_methods import CodexRpcMethods


class WsAppServerClient(CodexRpcMethods):
    """spawn `codex app-server --listen ws://127.0.0.1:PORT` 并经 WebSocket 连接。"""

    def __init__(self, settings: Settings, port: int = 0, *, endpoint: str = "",
                 bearer_token: str = "", token_file: str = "", spawn: bool = True):
        self.settings = settings
        self.requested_port = port or 8765
        self.endpoint = endpoint or f"ws://127.0.0.1:{self.requested_port}"
        self.bearer_token = bearer_token
        self.token_file = token_file
        self.spawn_local = spawn
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.initialize_result: dict = {}
        self.started_at: float = 0.0
        self.restart_count: int = 0
        self._next_id = 0
        self._pending: dict[Any, asyncio.Future] = {}
        self._read_task: Optional[asyncio.Task] = None
        self._stdout_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._message_tasks: set[asyncio.Task] = set()
        self._process_logs: deque[str] = deque(maxlen=100)
        self._job_handle: Optional[int] = None
        self._write_lock = asyncio.Lock()
        self._on_server_request: Optional[ServerRequestHandler] = None
        self._on_notification: Optional[NotificationHandler] = None
        self.api_capabilities: dict[str, bool] = {}
        self.api_compatible: Optional[bool] = None
        self.api_warning = ""
        self.runtime_command = ""
        self._runtime_cwd: Optional[tempfile.TemporaryDirectory[str]] = None

    def on_server_request(self, handler: ServerRequestHandler) -> None:
        self._on_server_request = handler

    def on_notification(self, handler: NotificationHandler) -> None:
        self._on_notification = handler

    # ---- 生命周期 ----
    async def start(self) -> None:
        if self.spawn_local:
            self._ensure_port_available()
            self._runtime_cwd = tempfile.TemporaryDirectory(
                prefix="chatcodex-appserver-"
            )
            command_prefix = resolve_codex_executable(self.settings.codex_command)
            self.runtime_command = (
                command_prefix[0] if len(command_prefix) == 1 else "")
            argv = command_prefix + [
                "app-server", "--listen", self.endpoint,
            ]
            if self.token_file:
                argv += [
                    "--ws-auth", "capability-token",
                    "--ws-token-file", self.token_file,
                ]
            self.proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=self._runtime_cwd.name,
            )
            self._attach_windows_kill_job()
            self._stdout_task = asyncio.create_task(self._drain(self.proc.stdout, "stdout"))
            self._stderr_task = asyncio.create_task(self._drain(self.proc.stderr, "stderr"))
        await self._connect_ws()
        self.started_at = time.time()
        self.initialize_result = await self.initialize()
        # JSON-RPC initialization is a two-step handshake. The server must not
        # receive normal requests until this notification has been sent.
        await self.notify("initialized", {})
        self.api_capabilities = await self._probe_official_api()
        required = ("commandExec",)
        missing = [name for name in required if not self.api_capabilities.get(name)]
        self.api_compatible = not missing
        if missing:
            self.api_warning = (
                "Connected Codex App Server is missing required official RPCs: "
                + ", ".join(missing)
                + ". Install a current official openai/codex release."
            )
        else:
            optional_missing = [
                name for name in (
                    "configRead", "configRequirements", "permissionProfiles"
                )
                if not self.api_capabilities.get(name)
            ]
            self.api_warning = (
                "Connected Codex App Server lacks optional standalone RPCs: "
                + ", ".join(optional_missing)
                + ". Affected operations fail closed or use conservative "
                  "Gateway policy."
                if optional_missing else ""
            )
        await asyncio.sleep(0)
        if self.proc and self.proc.returncode is not None:
            raise RuntimeError(
                f"spawned codex app-server exited with {self.proc.returncode}: "
                f"{' | '.join(self._process_logs)[-1000:]}")

    def _ensure_port_available(self) -> None:
        """Never attach to an unrelated app-server already on the port."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", self.requested_port))
            except OSError as exc:
                raise OSError(
                    f"codex app-server port {self.requested_port} is already in use; "
                    "choose a different CHATCODEX_CODEX_WS_PORT") from exc

    async def _drain(self, stream, label: str) -> None:
        if stream is None:
            return
        try:
            while line := await stream.readline():
                self._process_logs.append(
                    f"{label}: {line.decode('utf-8', 'replace').rstrip()}")
        except asyncio.CancelledError:
            return

    async def _connect_ws(self) -> None:
        url = self.endpoint
        last_err: Optional[Exception] = None
        attempts = 60 if self.spawn_local else 1
        for _ in range(attempts):
            try:
                kwargs: dict[str, Any] = {"max_size": None}
                if self.bearer_token:
                    header_key = (
                        "additional_headers"
                        if "additional_headers" in inspect.signature(
                            websockets.connect).parameters
                        else "extra_headers"
                    )
                    kwargs[header_key] = {
                        "Authorization": f"Bearer {self.bearer_token}"}
                self.ws = await websockets.connect(url, **kwargs)
                self._read_task = asyncio.create_task(self._read_loop())
                return
            except Exception as e:
                last_err = e
                await asyncio.sleep(0.1)
        raise ConnectionError(f"cannot connect app-server ws at {url}: {last_err}")

    async def _method_available(self, method: str, params: Any) -> bool:
        """A recognized method may reject harmless probe parameters and still exist."""
        try:
            await self.call(method, params, timeout=5)
            return True
        except Exception as exc:
            code = getattr(exc, "code", None)
            message = str(exc).lower()
            return code != -32601 and "method not found" not in message

    async def _probe_official_api(self) -> dict[str, bool]:
        """Probe only thread-free official methods without changing state."""
        probes = {
            "commandExec": ("command/exec", {"command": []}),
            "configRead": (
                "config/read", {"cwd": None, "includeLayers": False}),
            "configRequirements": ("configRequirements/read", None),
            "permissionProfiles": ("permissionProfile/list", {}),
        }
        results = await asyncio.gather(*(
            self._method_available(method, params)
            for method, params in probes.values()
        ))
        return dict(zip(probes, results))

    async def initialize(self) -> dict:
        return await self.call("initialize", {
            "clientInfo": {"name": CLIENT_NAME, "title": "ChatCodex Gateway", "version": CLIENT_VERSION},
            "capabilities": {"experimentalApi": True, "mcpServerOpenaiFormElicitation": True},
        })

    async def close(self) -> None:
        tasks = [task for task in (
            self._read_task, self._stdout_task, self._stderr_task) if task]
        if self._read_task:
            self._read_task.cancel()
        message_tasks = list(self._message_tasks)
        for task in message_tasks:
            task.cancel()
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), 5)
            except Exception:
                if self.proc.returncode is None:
                    try:
                        self.proc.kill()
                    except ProcessLookupError:
                        pass
                    # Do not close the event loop while the Windows subprocess
                    # transport is still waiting for process termination.
                    try:
                        await asyncio.wait_for(self.proc.wait(), 5)
                    except Exception:
                        pass
        self._close_windows_kill_job()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if message_tasks:
            await asyncio.gather(*message_tasks, return_exceptions=True)
        self._message_tasks.clear()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("closed"))
        self._pending.clear()
        self.ws = None
        self.proc = None
        self._read_task = None
        self._stdout_task = None
        self._stderr_task = None
        if self._runtime_cwd is not None:
            self._runtime_cwd.cleanup()
            self._runtime_cwd = None

    def _attach_windows_kill_job(self) -> None:
        """Kill app-server and descendants if the Gateway process disappears.

        asyncio's normal ``Process.terminate`` handles an orderly shutdown, but
        Windows otherwise leaves a child running when its parent is forcibly
        terminated. A kill-on-close Job Object makes the OS own that cleanup.
        Failure is non-fatal because some restricted Windows hosts do not allow
        assigning a process to a nested job.
        """
        if self.proc is not None:
            self._job_handle = attach_windows_kill_job(
                self.proc.pid, self._process_logs.append)

    def _close_windows_kill_job(self) -> None:
        close_windows_kill_job(self._job_handle)
        self._job_handle = None

    async def restart(self) -> dict:
        await self.close()
        self.restart_count += 1
        await self.start()
        return self.status()

    def status(self) -> dict:
        connected = self.ws is not None and not bool(getattr(self.ws, "closed", False))
        running = connected and (
            not self.spawn_local or (self.proc is not None and self.proc.returncode is None))
        return {
            "running": running,
            "pid": self.proc.pid if self.proc else None,
            "mode": "internal" if self.spawn_local else "external",
            "listen": self.endpoint,
            "command": (
                self.runtime_command or self.settings.codex_command
                if self.spawn_local else ""),
            "userAgent": self.initialize_result.get("userAgent", ""),
            "codexHome": self.initialize_result.get("codexHome", ""),
            "platformFamily": self.initialize_result.get("platformFamily", ""),
            "platformOs": self.initialize_result.get("platformOs", ""),
            "apiCapabilities": dict(self.api_capabilities),
            "apiCompatible": self.api_compatible,
            "apiWarning": self.api_warning,
            "uptimeSec": int(time.time() - self.started_at) if running and self.started_at else 0,
            "restartCount": self.restart_count,
            "logs": list(self._process_logs)[-20:],
        }

    # ---- JSON-RPC over ws ----
    def _alloc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def call(self, method: str, params: Any = None, *, timeout: float = 120.0) -> Any:
        rid = self._alloc_id()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: Any = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _respond(self, rid: Any, result: Any = None, error: Optional[dict] = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        await self._send(msg)

    async def _send(self, obj: dict) -> None:
        ws = self.ws
        if ws is None:
            raise ConnectionError("app-server WebSocket is not connected")
        async with self._write_lock:
            await ws.send(json.dumps(obj, ensure_ascii=False))

    async def _read_loop(self) -> None:
        ws = self.ws
        if ws is None:
            raise ConnectionError("app-server WebSocket is not connected")
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("ws closed"))

    def _dispatch(self, msg: dict) -> None:
        # 响应
        if "method" not in msg and "id" in msg:
            fut = self._pending.get(msg["id"])
            if fut and not fut.done():
                if "error" in msg:
                    e = msg["error"] or {}
                    from .jsonrpc import JsonRpcError
                    fut.set_exception(JsonRpcError(e.get("code", -32000), e.get("message", "error"), e.get("data")))
                else:
                    fut.set_result(msg.get("result"))
            return
        # 反向 request / 通知 → 独立 task,不阻塞读循环
        if "method" in msg and "id" in msg:
            self._track_message_task(self._handle_server_request(msg))
            return
        if "method" in msg:
            if self._on_notification:
                self._track_message_task(self._safe_notify(msg))

    def _track_message_task(self, coro) -> None:
        """Keep reverse-RPC tasks bounded by the WebSocket lifecycle."""
        task = asyncio.create_task(coro)
        self._message_tasks.add(task)

        def done(completed: asyncio.Task) -> None:
            self._message_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)

    async def _handle_server_request(self, msg: dict) -> None:
        if self._on_server_request:
            try:
                result = await self._on_server_request(msg)
                await self._respond(msg["id"], result=result)
            except Exception as exc:
                await self._respond(msg["id"], error={"code": -32000, "message": str(exc)})
        else:
            await self._respond(msg["id"], error={"code": -32601, "message": "method not found"})

    async def _safe_notify(self, msg: dict) -> None:
        try:
            await self._on_notification(msg["method"], msg.get("params"))  # type: ignore[misc]
        except Exception:
            pass
