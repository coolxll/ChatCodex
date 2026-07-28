"""公网入口与 MCP Secure Tunnel 的进程管理。

三种底层传输:
  direct      — 直接公网暴露(机器有公网 IP / 端口映射),不跑隧道进程。
  cloudflared — named tunnel + JWT(固定域名,生产) 或 trycloudflare(临时随机域名,联调)。
  chatgpt     — OpenAI 官方 tunnel-client(控制面长轮询,无公网入口)。

direct/cloudflared 只能作为全局公网入口；chatgpt 只能由独立 MCP
Tunnel API 启动。底层 manager 仍统一负责线程隔离、守护与状态聚合。
"""
from __future__ import annotations

import asyncio
from collections import deque
import concurrent.futures
import os
import re
import shutil
import tempfile
import threading
from typing import Any, Callable, Optional
import urllib.request
from urllib.parse import urlsplit

from ..config import Settings
from ..native import NativeRuntimeManager
from ..process_guard import attach_windows_kill_job, close_windows_kill_job

# trycloudflare 输出形如: https://xxxx-yyyy.trycloudflare.com
_TRY_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I)


class TunnelStatus(dict):
    def __init__(self, kind: str, running: bool, url: str = "", detail: str = "",
                 pid: int = 0, **extra):
        super().__init__(kind=kind, running=running, url=url, detail=detail, pid=pid,
                         **extra)


class BaseTunnel:
    kind = "base"

    def __init__(self, settings: Settings,
                 on_public_url: Optional[Callable[[str], None]] = None):
        self.settings = settings
        self.on_public_url = on_public_url
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.url = ""
        self.detail = ""
        self.logs: deque[str] = deque(maxlen=100)
        self.exit_code: Optional[int] = None
        self._job_handle: Optional[int] = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def status(self) -> TunnelStatus:
        return TunnelStatus(self.kind, self.running(), self.url, self.detail,
                            self.proc.pid if self.proc else 0)

    async def start(self) -> TunnelStatus:  # pragma: no cover
        raise NotImplementedError

    async def stop(self) -> None:
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
                    try:
                        await asyncio.wait_for(self.proc.wait(), 5)
                    except Exception:
                        pass
        self._close_windows_kill_job()
        self.proc = None

    def _attach_windows_kill_job(self) -> None:
        if self.proc is not None:
            self._job_handle = attach_windows_kill_job(
                self.proc.pid, self.logs.append)

    def _close_windows_kill_job(self) -> None:
        close_windows_kill_job(self._job_handle)
        self._job_handle = None

    def _target(self) -> str:
        host = self.settings.host
        # This is a comparison, not a network bind.
        if host in ("0.0.0.0", "::", "[::]"):  # nosec B104
            host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.settings.port}"

    def _publish_public_url(self) -> None:
        if not self.url or self.on_public_url is None:
            return
        try:
            self.on_public_url(self.url)
        except Exception as exc:
            self.logs.append(f"public URL activation failed: {exc}")


class IsolatedTunnel:
    """Own one tunnel runtime on a dedicated thread and asyncio event loop."""

    def __init__(self, tunnel: BaseTunnel, instance_id: str):
        self.tunnel = tunnel
        self.instance_id = instance_id
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._caller_loop: Optional[asyncio.AbstractEventLoop] = None
        self._on_public_url = tunnel.on_public_url
        if self._on_public_url is not None:
            tunnel.on_public_url = self._publish_public_url_on_caller

    def _publish_public_url_on_caller(self, public_url: str) -> None:
        """Serialize runtime OAuth/widget mutation onto the Gateway loop."""
        callback = self._on_public_url
        caller = self._caller_loop
        if callback is None:
            return
        if caller is None:
            callback(public_url)
            return
        completed: concurrent.futures.Future[None] = concurrent.futures.Future()

        def invoke() -> None:
            try:
                callback(public_url)
            except BaseException as exc:
                completed.set_exception(exc)
            else:
                completed.set_result(None)

        caller.call_soon_threadsafe(invoke)
        completed.result(timeout=10)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self.tunnel.start())
            self._ready.set()
            loop.run_forever()
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()
        finally:
            try:
                loop.run_until_complete(self.tunnel.stop())
            except Exception:
                pass
            loop.close()

    async def start(self) -> TunnelStatus:
        self._caller_loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f"chatgpt-tunnel-{self.instance_id}")
        self._thread.start()
        while not self._ready.is_set():
            await asyncio.sleep(0.05)
        if self._start_error:
            raise self._start_error
        return self.status()

    async def stop(self) -> None:
        loop, thread = self._loop, self._thread
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.tunnel.stop(), loop)
            try:
                await asyncio.wait_for(asyncio.wrap_future(future), 15)
            except Exception:
                future.cancel()
            finally:
                loop.call_soon_threadsafe(loop.stop)
        if thread:
            await asyncio.to_thread(thread.join, 10)
        self._loop = None
        self._thread = None
        self._caller_loop = None

    def status(self) -> TunnelStatus:
        return TunnelStatus(**{
            **self.tunnel.status(),
            "instanceId": self.instance_id,
            "threadIsolated": True,
            "threadName": self._thread.name if self._thread else "",
        })


class DirectTunnel(BaseTunnel):
    """直接公网暴露:无需进程,公网地址即 public_url。"""
    kind = "direct"

    def __init__(self, settings: Settings,
                 on_public_url: Optional[Callable[[str], None]] = None):
        super().__init__(settings, on_public_url=on_public_url)
        self._up = False

    def running(self) -> bool:
        return self._up

    async def start(self) -> TunnelStatus:
        self.url = self.settings.public_url
        self._publish_public_url()
        self.detail = "no tunnel process; assumes the host is publicly reachable"
        self._up = True
        return self.status()

    async def stop(self) -> None:
        self._up = False


class CloudflaredTunnel(BaseTunnel):
    """cloudflared:named+JWT(生产)或 trycloudflare(联调)。"""
    kind = "cloudflared"

    def __init__(self, settings: Settings, mode: str = "try", token: str = "",
                 on_public_url: Optional[Callable[[str], None]] = None):
        super().__init__(settings, on_public_url=on_public_url)
        self.mode = mode            # "try" | "named"
        self.token = token
        self._url_ready: Optional[asyncio.Event] = None
        self._reader_task: Optional[asyncio.Task] = None

    async def start(self) -> TunnelStatus:
        exe = shutil.which("cloudflared")
        if not exe:
            self.detail = "cloudflared not found in PATH"
            return self.status()
        if self.mode == "named":
            if not self.token:
                self.detail = "named tunnel requires a token (JWT)"
                return self.status()
            # cloudflared supports TUNNEL_TOKEN. Keep the credential out of the
            # process command line, which is readable through local process
            # inspection on Windows and many Unix systems.
            argv = [exe, "tunnel", "run"]
            child_env = {**os.environ, "TUNNEL_TOKEN": self.token}
            # named tunnel 的公网域名在 Cloudflare 侧配置;此处用 public_url 作为展示
            self.url = self.settings.public_url
            self._publish_public_url()
            self.detail = "named tunnel (token); hostname configured in Cloudflare dashboard"
        else:
            argv = [exe, "tunnel", "--url", self._target(), "--no-autoupdate"]
            child_env = None
            self.detail = "trycloudflare (ephemeral)"
        self.proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=child_env)
        self._attach_windows_kill_job()
        if self.mode == "try":
            self._url_ready = asyncio.Event()
            self._reader_task = asyncio.create_task(self._capture_try_url())
            # The ephemeral hostname is part of the running OAuth contract, so
            # return it with the start result when cloudflared emits it quickly.
            try:
                await asyncio.wait_for(self._url_ready.wait(), 10)
            except TimeoutError:
                self.detail = "trycloudflare running; waiting for public URL"
        return self.status()

    async def stop(self) -> None:
        task = self._reader_task
        if task:
            task.cancel()
        await super().stop()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        self._reader_task = None
        self._url_ready = None

    async def _capture_try_url(self) -> None:
        if not self.proc or not self.proc.stdout:
            return
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", "replace").rstrip()
                self.logs.append(decoded)
                m = _TRY_URL_RE.search(decoded)
                if m:
                    self.url = m.group(0)
                    self.detail = "trycloudflare (ephemeral)"
                    self._publish_public_url()
                    if self._url_ready is not None:
                        self._url_ready.set()
        except Exception:
            pass


def _chatgpt_tunnel_oauth_warning(settings: Settings) -> str:
    if settings.mcp_auth_mode not in ("oauth", "both"):
        return ""
    issuer = settings.public_url.rstrip("/")
    try:
        parsed = urlsplit(issuer)
    except ValueError:
        parsed = None
    if (not parsed or parsed.scheme != "https" or not parsed.hostname or
            parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}):
        return (
            "OAuth through Secure MCP Tunnel requires authorization_servers[0] "
            "to be a publicly reachable HTTPS issuer. Configure the Gateway's "
            "global direct/Cloudflare public URL first. The tunnel rewrites MCP "
            "resource metadata, but it does not tunnel the authorization server."
        )
    return ""


class ChatGptTunnel(BaseTunnel):
    """OpenAI 官方 tunnel-client(控制面长轮询)。"""
    kind = "chatgpt"

    def __init__(self, settings: Settings, tunnel_id: str = "", api_key: str = "",
                 client_bin: str = "tunnel-client"):
        super().__init__(settings)
        self.tunnel_id = tunnel_id
        self.api_key = api_key
        self.client_bin = client_bin
        self.health_url = ""
        self.healthy = False
        self.ready = False
        self._health_file = ""
        self._reader_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None

    def status(self) -> TunnelStatus:
        oauth_warning = _chatgpt_tunnel_oauth_warning(self.settings)
        return TunnelStatus(
            self.kind, self.running(), self.url, self.detail,
            self.proc.pid if self.proc else 0,
            tunnelId=self.tunnel_id,
            healthUrl=self.health_url,
            healthy=self.healthy,
            ready=self.ready,
            exitCode=self.exit_code,
            logs=list(self.logs)[-20:],
            oauthRequired=self.settings.mcp_auth_mode in ("oauth", "both"),
            oauthIssuer=self.settings.public_url,
            oauthCompatible=not bool(oauth_warning),
            oauthWarning=oauth_warning,
        )

    def _resolve_executable(self) -> Optional[str]:
        if os.path.isfile(self.client_bin):
            return os.path.abspath(self.client_bin)
        return shutil.which(self.client_bin)

    async def start(self) -> TunnelStatus:
        exe = self._resolve_executable()
        if not exe:
            self.detail = ("tunnel-client not found; install/build it or set "
                           "CHATCODEX_TUNNEL_CLIENT to the executable path")
            return self.status()
        if not (self.tunnel_id and self.api_key):
            self.detail = "chatgpt tunnel requires CONTROL_PLANE_TUNNEL_ID + CONTROL_PLANE_API_KEY"
            return self.status()
        oauth_warning = _chatgpt_tunnel_oauth_warning(self.settings)
        if oauth_warning:
            self.detail = oauth_warning
            return self.status()
        # 对齐 tunnel-client 真实 CLI(ref/tunnel-client/docs/configuration.md)
        fd, self._health_file = tempfile.mkstemp(prefix="chatcodex-tunnel-health-", suffix=".url")
        os.close(fd)
        argv = [
            exe, "run",
            "--control-plane.tunnel-id", self.tunnel_id,
            "--control-plane.api-key", "env:CONTROL_PLANE_API_KEY",
            # FastMCP is mounted at /mcp/; use the canonical trailing-slash URL
            # so the startup POST is not redirected before authentication.
            "--mcp.server-url", f"{self._target()}/mcp/",
            "--health.listen-addr", "127.0.0.1:0",
            "--health.url-file", self._health_file,
        ]
        merged = {**os.environ,
                  "CONTROL_PLANE_API_KEY": self.api_key,
                  "CONTROL_PLANE_TUNNEL_ID": self.tunnel_id}
        if (self.settings.mcp_auth_mode in ("token", "both") and
                self.settings.mcp_access_token):
            # Authenticate the private tunnel-client -> MCP hop without putting
            # the secret in argv or the public ChatGPT connector config.
            merged["CHATCODEX_MCP_AUTH"] = f"Bearer {self.settings.mcp_access_token}"
            argv += [
                "--mcp.extra-headers", "Authorization: env:CHATCODEX_MCP_AUTH",
                "--mcp.discovery-extra-headers", "Authorization: env:CHATCODEX_MCP_AUTH",
            ]
        self.proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=merged)
        self._attach_windows_kill_job()
        self.url = f"tunnel:{self.tunnel_id}"
        self.detail = "starting OpenAI Secure MCP Tunnel"
        self._reader_task = asyncio.create_task(self._read_logs())
        self._monitor_task = asyncio.create_task(self._monitor())
        await self._wait_for_health_url()
        await self._probe()
        if self.ready:
            self.detail = "ready; select this tunnel ID when creating the ChatGPT app"
        elif self.running():
            self.detail = "process is running but tunnel is not ready yet"
        return self.status()

    async def stop(self) -> None:
        tasks = [task for task in (self._reader_task, self._monitor_task) if task]
        for task in tasks:
            if task:
                task.cancel()
        await super().stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._monitor_task = None
        if self._health_file:
            try:
                os.unlink(self._health_file)
            except OSError:
                pass
        self.health_url = ""
        self.healthy = False
        self.ready = False

    async def _read_logs(self) -> None:
        if not self.proc or not self.proc.stdout:
            return
        try:
            while line := await self.proc.stdout.readline():
                self.logs.append(line.decode("utf-8", "replace").rstrip())
        except asyncio.CancelledError:
            return

    async def _wait_for_health_url(self) -> None:
        for _ in range(50):
            if not self.running():
                return
            try:
                value = await asyncio.to_thread(
                    lambda: open(self._health_file, "r", encoding="utf-8").read().strip())
                if value and self._valid_health_url(value):
                    self.health_url = value.rstrip("/")
                    return
            except OSError:
                pass
            await asyncio.sleep(0.1)

    async def _monitor(self) -> None:
        try:
            while self.running():
                await self._probe()
                await asyncio.sleep(2)
            if self.proc:
                self.exit_code = self.proc.returncode
                self.detail = f"tunnel-client exited with code {self.exit_code}"
                self.healthy = False
                self.ready = False
        except asyncio.CancelledError:
            return

    async def _probe(self) -> None:
        if not self.health_url:
            return
        self.healthy = await asyncio.to_thread(self._http_ok, f"{self.health_url}/healthz")
        self.ready = await asyncio.to_thread(self._http_ok, f"{self.health_url}/readyz")

    @staticmethod
    def _http_ok(url: str) -> bool:
        if not ChatGptTunnel._valid_health_url(url):
            return False
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:  # nosec B310
                return 200 <= response.status < 300
        except Exception:
            return False

    @staticmethod
    def _valid_health_url(url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (
                parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            return False


class TunnelManager:
    """Manage isolated named tunnel runtimes with bounded crash recovery."""

    RESTART_BACKOFF = [2, 5, 15, 60]

    def __init__(self, settings: Settings, native: Optional[NativeRuntimeManager] = None,
                 on_public_url: Optional[Callable[[str], None]] = None):
        self.settings = settings
        self.native = native or NativeRuntimeManager(settings.native_dir)
        self.on_public_url = on_public_url
        self.instances: dict[str, IsolatedTunnel] = {}
        self._specs: dict[str, tuple[str, dict[str, Any]]] = {}
        self._watchdogs: dict[str, asyncio.Task] = {}
        self._restart_attempts: dict[str, int] = {}
        self._unhealthy_counts: dict[str, int] = {}
        self._last_errors: dict[str, str] = {}
        self._lifecycle_lock = asyncio.Lock()

    def _build(self, kind: str, **kw) -> BaseTunnel:
        if kind == "cloudflared":
            return CloudflaredTunnel(
                self.settings, mode=kw.get("mode", "try"),
                token=kw.get("token", ""), on_public_url=self.on_public_url)
        if kind == "chatgpt":
            return ChatGptTunnel(self.settings,
                                 tunnel_id=(kw.get("tunnel_id") or
                                            self.settings.chatgpt_tunnel_id),
                                 api_key=(kw.get("api_key") or
                                          self.settings.chatgpt_api_key),
                                 client_bin=kw.get("client_bin") or
                                            self.settings.tunnel_client_command)
        return DirectTunnel(self.settings, on_public_url=self.on_public_url)

    async def start(self, kind: str, **kw) -> TunnelStatus:
        async with self._lifecycle_lock:
            return await self._start_unlocked(kind, **kw)

    async def _start_unlocked(self, kind: str, **kw) -> TunnelStatus:
        instance_id = str(kw.pop("instance_id", "default") or "default")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", instance_id):
            raise ValueError("tunnel instance_id must be 1-64 safe identifier characters")
        await self._stop_unlocked(instance_id)
        if kind == "chatgpt":
            kw["client_bin"] = await self._resolve_tunnel_client(
                kw.get("client_bin", ""))
        tunnel = self._build(kind, **kw)
        if kind != "chatgpt" and self.settings.mcp_auth_mode == "noauth":
            tunnel.detail = (
                "refusing public/direct tunnel while MCP_AUTH_MODE=noauth; "
                "use the authenticated ChatGPT tunnel or enable token/OAuth")
            return TunnelStatus(**{
                **tunnel.status(), "instanceId": instance_id,
                "threadIsolated": True})
        isolated = IsolatedTunnel(tunnel, instance_id)
        self.instances[instance_id] = isolated
        self._specs[instance_id] = (kind, dict(kw))
        self._restart_attempts[instance_id] = 0
        self._unhealthy_counts[instance_id] = 0
        result = await isolated.start()
        if result.get("running"):
            self._watchdogs[instance_id] = asyncio.create_task(
                self._watchdog(instance_id))
        else:
            self._last_errors[instance_id] = str(result.get("detail") or "tunnel did not start")[:300]
        return self._with_manager_state(instance_id, result)

    async def _resolve_tunnel_client(self, configured: str) -> str:
        candidate = configured or self.settings.tunnel_client_command
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
        found = shutil.which(candidate)
        if found:
            return found
        installed = self.native.tunnel_command()
        if installed:
            return installed
        result = await asyncio.to_thread(
            self.native.install_tunnel_client,
            self.settings.tunnel_client_release)
        return result["tunnelCommand"]

    async def _watchdog(self, instance_id: str) -> None:
        try:
            while instance_id in self.instances:
                await asyncio.sleep(5)
                isolated = self.instances.get(instance_id)
                if not isolated:
                    return
                current_status = isolated.status()
                running = bool(current_status.get("running"))
                healthy = current_status.get("healthy")
                if running and healthy is not False:
                    self._restart_attempts[instance_id] = 0
                    self._unhealthy_counts[instance_id] = 0
                    continue
                if running and healthy is False:
                    failures = self._unhealthy_counts.get(instance_id, 0) + 1
                    self._unhealthy_counts[instance_id] = failures
                    if failures < 6:
                        continue
                if not self.settings.tunnel_auto_restart:
                    return
                attempts = self._restart_attempts.get(instance_id, 0)
                if attempts >= len(self.RESTART_BACKOFF):
                    self._last_errors[instance_id] = "maximum restart attempts reached"
                    return
                await asyncio.sleep(self.RESTART_BACKOFF[attempts])
                async with self._lifecycle_lock:
                    # A user stop/start may have replaced this instance during
                    # the backoff. Never resurrect or overwrite that runtime.
                    if self.instances.get(instance_id) is not isolated:
                        continue
                    stored = self._specs.get(instance_id)
                    if stored is None:
                        return
                    self._restart_attempts[instance_id] = attempts + 1
                    kind, spec = stored
                    try:
                        await isolated.stop()
                        replacement = IsolatedTunnel(
                            self._build(kind, **spec), instance_id)
                        self.instances[instance_id] = replacement
                        await replacement.start()
                        self._last_errors[instance_id] = ""
                        self._unhealthy_counts[instance_id] = 0
                    except Exception as exc:
                        self._last_errors[instance_id] = str(exc)[:300]
        except asyncio.CancelledError:
            return

    async def stop(self, instance_id: Optional[str] = None) -> None:
        async with self._lifecycle_lock:
            await self._stop_unlocked(instance_id)

    async def _stop_unlocked(self, instance_id: Optional[str] = None) -> None:
        ids = [instance_id] if instance_id else list(self.instances)
        for key in ids:
            task = self._watchdogs.pop(key, None)
            if task and task is not asyncio.current_task():
                task.cancel()
            isolated = self.instances.pop(key, None)
            if isolated:
                await isolated.stop()
            self._specs.pop(key, None)
            self._restart_attempts.pop(key, None)
            self._unhealthy_counts.pop(key, None)

    def _with_manager_state(self, instance_id: str, status: dict) -> TunnelStatus:
        return TunnelStatus(**{
            **status,
            "autoRestart": self.settings.tunnel_auto_restart,
            "restartAttempts": self._restart_attempts.get(instance_id, 0),
            "consecutiveFailures": self._unhealthy_counts.get(instance_id, 0),
            "lastError": self._last_errors.get(instance_id, ""),
        })

    def status(self, instance_id: Optional[str] = None) -> TunnelStatus:
        statuses = [
            dict(self._with_manager_state(key, isolated.status()))
            for key, isolated in sorted(self.instances.items())
        ]
        selected = (next(
            (item for item in statuses if item.get("instanceId") == instance_id),
            None) if instance_id else (statuses[0] if statuses else None))
        if selected:
            return TunnelStatus(**{**selected, "instances": statuses})
        return TunnelStatus(
            "none", False,
            detail=(f"tunnel instance {instance_id!r} is not running"
                    if instance_id else "no tunnel started"),
            instances=statuses)
