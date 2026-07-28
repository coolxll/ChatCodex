"""Codex App Server RPC helpers.

这些方法内部只调 self.call/self.notify,与传输无关。
ws_client(WsAppServerClient)继承本类获得全部高层方法。

ChatCodex exposes only standalone fs/search/command/config methods here.  Agent
thread and turn methods are intentionally absent so WebChat cannot accidentally
start a second model loop through this adapter.
"""
from __future__ import annotations

from typing import Optional


class CodexRpcMethods:
    # 子类需提供: async def call(method, params, *, timeout) / notify(method, params)

    # ---- fs ----
    async def fs_read_file(self, path: str) -> dict:
        return await self.call("fs/readFile", {"path": path})

    async def fs_write_file(self, path: str, data_base64: str) -> dict:
        return await self.call("fs/writeFile", {"path": path, "dataBase64": data_base64})

    async def fs_read_directory(self, path: str) -> dict:
        return await self.call("fs/readDirectory", {"path": path})

    async def fs_get_metadata(self, path: str) -> dict:
        return await self.call("fs/getMetadata", {"path": path})

    # ---- exec / search / shell ----
    async def exec_command(self, command: list[str], cwd: Optional[str] = None,
                           timeout_ms: Optional[int] = None,
                           sandbox_policy: Optional[dict] = None,
                           permission_profile_id: Optional[str] = None) -> dict:
        if sandbox_policy is not None and permission_profile_id:
            raise ValueError(
                "command/exec accepts sandboxPolicy or permissionProfile, not both")
        params: dict = {"command": command}
        if cwd:
            params["cwd"] = cwd
        if timeout_ms is not None:
            params["timeoutMs"] = timeout_ms
        if sandbox_policy is not None:
            params["sandboxPolicy"] = sandbox_policy
        if permission_profile_id:
            params["permissionProfile"] = permission_profile_id
        return await self.call("command/exec", params)

    async def fuzzy_search(self, query: str, roots: list[str]) -> dict:
        return await self.call("fuzzyFileSearch", {
            "query": query,
            "roots": roots,
            "cancellationToken": None,
        })
