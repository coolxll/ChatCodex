"""AppServer 隔离运行时:在专属线程跑专属事件循环,与 uvicorn 的 ProactorEventLoop 隔离。

背景:uvicorn 在 Windows 的 ProactorEventLoop 下,子进程 stdio 的 readline 在
并发请求阶段会停止投递(已知 asyncio/uvicorn 兼容问题),导致 initialize 之后
的 RPC 全部假死。把 app-server 客户端放到独立线程+独立 loop 即可彻底规避。

对外提供与客户端相同的协程接口,内部经 run_coroutine_threadsafe 转发。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Awaitable, Optional

from ..config import Settings


class IsolatedAppServer:
    """在专属线程运行一个 app-server 客户端(ws);协程接口与之一致。"""

    def __init__(self, settings: Settings, client: Any):
        if client is None:
            raise ValueError("IsolatedAppServer requires a client (e.g. WsAppServerClient)")
        self.settings = settings
        self._client = client
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._start_error: Optional[BaseException] = None
        # 主线程(uvicorn)loop,用于把子线程的回调桥回主线程
        self._caller_loop: Optional[asyncio.AbstractEventLoop] = None

    # ---- 生命周期 ----
    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._client.start())
            self._ready.set()
            loop.run_forever()
        except BaseException as e:  # 启动失败
            self._start_error = e
            self._ready.set()
        finally:
            try:
                loop.run_until_complete(self._client.close())
            except Exception:
                pass
            loop.close()

    async def start(self) -> None:
        # 记录主线程 loop,供回调桥接
        try:
            self._caller_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._caller_loop = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="codex-appserver")
        self._thread.start()
        # 等待子线程里 client.start() 完成(跨线程等待,轮询避免阻塞本 loop)
        while not self._ready.is_set():
            await asyncio.sleep(0.05)
        if self._start_error:
            raise self._start_error

    async def close(self) -> None:
        loop = self._loop
        thread = self._thread
        if loop and loop.is_running():
            # Close WebSocket/subprocess transports while their owning event
            # loop is still running. Stopping the loop first can orphan the
            # Windows codex.exe child before proc.wait() is serviced.
            future = asyncio.run_coroutine_threadsafe(self._client.close(), loop)
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

    # ---- 回调注册:把子线程 loop 里的调用桥回主线程 loop ----
    def on_server_request(self, handler) -> None:
        async def bridged(msg: dict) -> dict:
            caller = self._caller_loop
            if caller is None:
                return await handler(msg)
            # 子线程收到反向 request → 提交到主线程执行 handler → 跨线程等结果
            fut = asyncio.run_coroutine_threadsafe(handler(msg), caller)
            return await asyncio.wrap_future(fut)

        self._client.on_server_request(bridged)

    def on_notification(self, handler) -> None:
        async def bridged(method: str, params) -> None:
            caller = self._caller_loop
            if caller is None:
                await handler(method, params)
                return
            caller.call_soon_threadsafe(lambda: asyncio.ensure_future(handler(method, params)))

        self._client.on_notification(bridged)

    # ---- 状态/属性代理 ----
    @property
    def proc(self):
        return self._client.proc

    @property
    def initialize_result(self) -> dict:
        return self._client.initialize_result

    def status(self) -> dict:
        return self._client.status()

    async def restart(self) -> dict:
        return await self._submit(self._client.restart())

    # ---- 通用转发:把协程提交到子线程 loop,并在本 loop 等待结果 ----
    async def _submit(self, coro: Awaitable[Any]) -> Any:
        loop = self._loop
        if loop is None or not loop.is_running():
            if hasattr(coro, "close"):
                coro.close()
            raise RuntimeError("appserver loop not running")
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return await asyncio.wrap_future(fut)

    def call(self, method: str, params: Any = None, *, timeout: float = 120.0) -> Awaitable[Any]:
        return self._submit(self._client.call(method, params, timeout=timeout))

    # ---- 高层 RPC 代理(与客户端同名) ----
    def __getattr__(self, name: str):
        """把 client 的独立 RPC 协程方法透明转发到隔离事件循环。"""
        attr = getattr(self._client, name)
        if not callable(attr) or name.startswith("_"):
            return attr

        async def forward(*args, **kwargs):
            return await self._submit(attr(*args, **kwargs))
        return forward
