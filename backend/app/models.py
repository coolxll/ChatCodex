"""Persistent thread-free WebChat execution contexts."""
from __future__ import annotations

import json
import platform
import secrets
import time
from dataclasses import dataclass, asdict
from typing import Optional

from .db import Database


def _now() -> int:
    return int(time.time())


@dataclass
class ExecutionContext:
    """Security authority for one WebChat conversation.

    This record deliberately has no Codex thread or turn identifier.  The
    official App Server is used only for standalone execution RPCs.
    """

    id: str
    map_key: str
    conversation_id: str
    user_id: str
    cwd: str
    workspace_roots: str
    sandbox_mode: str = "workspace-write"
    permission_profile_id: Optional[str] = None
    approval_policy: str = "on-request"
    approvals_reviewer: str = "user"
    work_mode: str = "agent"
    platform: str = ""
    appserver_instance_id: str = ""
    version: int = 1
    status: str = "active"
    created_at: int = 0
    updated_at: int = 0

    def roots(self) -> list[str]:
        try:
            roots = json.loads(self.workspace_roots)
        except Exception:
            return []
        return [str(root) for root in roots] if isinstance(roots, list) else []

class ExecutionRegistry:
    """CRUD for thread-free execution contexts."""

    def __init__(self, db: Database):
        self.db = db

    def get_by_map_key(self, map_key: str) -> Optional[ExecutionContext]:
        with self.db.conn() as c:
            row = c.execute(
                "SELECT * FROM execution_context WHERE map_key=?", (map_key,)
            ).fetchone()
        return self._to(row) if row else None

    def get_by_conversation(
            self, conversation_id: str, user_id: Optional[str] = None
    ) -> Optional[ExecutionContext]:
        query = "SELECT * FROM execution_context WHERE conversation_id=?"
        args: list[object] = [conversation_id]
        if user_id:
            query += " AND user_id=?"
            args.append(user_id)
        query += " ORDER BY updated_at DESC LIMIT 1"
        with self.db.conn() as c:
            row = c.execute(query, args).fetchone()
        return self._to(row) if row else None

    def get(self, context_id: str) -> Optional[ExecutionContext]:
        with self.db.conn() as c:
            row = c.execute(
                "SELECT * FROM execution_context WHERE id=?", (context_id,)
            ).fetchone()
        return self._to(row) if row else None

    def list(self, user_id: Optional[str] = None,
             status: Optional[str] = None) -> list[ExecutionContext]:
        query = "SELECT * FROM execution_context WHERE 1=1"
        args: list[object] = []
        if user_id:
            query += " AND user_id=?"
            args.append(user_id)
        if status:
            query += " AND status=?"
            args.append(status)
        query += " ORDER BY updated_at DESC"
        with self.db.conn() as c:
            rows = c.execute(query, args).fetchall()
        return [self._to(row) for row in rows]

    def configure(
            self, *, map_key: str, conversation_id: str, user_id: str,
            cwd: str, workspace_roots: list[str], sandbox_mode: str,
            permission_profile_id: Optional[str], approval_policy: str,
            work_mode: str, platform_name: str = "",
            appserver_instance_id: str = ""
    ) -> ExecutionContext:
        now = _now()
        roots_json = json.dumps(workspace_roots, ensure_ascii=False)
        target_platform = (
            str(platform_name).strip().lower() or platform.system().lower()
        )
        existing = self.get_by_map_key(map_key)
        if existing:
            version = int(existing.version or 0) + 1
            with self.db.conn() as c:
                c.execute(
                    """UPDATE execution_context
                       SET conversation_id=?,user_id=?,cwd=?,workspace_roots=?,
                           sandbox_mode=?,permission_profile_id=?,approval_policy=?,
                           approvals_reviewer='user',work_mode=?,platform=?,
                           appserver_instance_id=?,version=?,status='active',updated_at=?
                       WHERE id=?""",
                    (conversation_id, user_id, cwd, roots_json, sandbox_mode,
                     permission_profile_id, approval_policy, work_mode,
                     target_platform, appserver_instance_id, version,
                     now, existing.id),
                )
            return self.get(existing.id)  # type: ignore[return-value]

        context_id = secrets.token_hex(12)
        with self.db.conn() as c:
            c.execute(
                """INSERT INTO execution_context
                   (id,map_key,conversation_id,user_id,cwd,workspace_roots,
                    sandbox_mode,permission_profile_id,approval_policy,
                    approvals_reviewer,work_mode,platform,appserver_instance_id,
                    version,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,'user',?,?,?,?, 'active',?,?)""",
                (context_id, map_key, conversation_id, user_id, cwd, roots_json,
                 sandbox_mode, permission_profile_id, approval_policy, work_mode,
                 target_platform, appserver_instance_id, 1, now, now),
            )
        return self.get(context_id)  # type: ignore[return-value]

    def set_status(self, context_id: str, status: str) -> Optional[ExecutionContext]:
        with self.db.conn() as c:
            c.execute(
                """UPDATE execution_context
                   SET status=?,version=version+1,updated_at=? WHERE id=?""",
                (status, _now(), context_id),
            )
        return self.get(context_id)

    def invalidate_appserver_instance(self) -> int:
        """Bump authority versions when the standalone RPC host is replaced."""
        with self.db.conn() as c:
            cursor = c.execute(
                """UPDATE execution_context
                   SET appserver_instance_id='',version=version+1,updated_at=?
                   WHERE status='active'""",
                (_now(),),
            )
        return int(cursor.rowcount or 0)

    def delete(self, context_id: str) -> None:
        with self.db.conn() as c:
            c.execute("DELETE FROM execution_context WHERE id=?", (context_id,))

    @staticmethod
    def _to(row) -> ExecutionContext:
        return ExecutionContext(**dict(row))

    @staticmethod
    def to_dict(context: ExecutionContext) -> dict:
        data = asdict(context)
        data["workspaceRoots"] = context.roots()
        data["sandboxMode"] = data.pop("sandbox_mode")
        data["permissionProfileId"] = data.pop("permission_profile_id")
        data["approvalPolicy"] = data.pop("approval_policy")
        data["approvalsReviewer"] = data.pop("approvals_reviewer")
        data["workMode"] = data.pop("work_mode")
        data["appServerInstanceId"] = data.pop("appserver_instance_id")
        data["conversationId"] = data.pop("conversation_id")
        data.pop("workspace_roots", None)
        data.pop("map_key", None)
        data.pop("user_id", None)
        return data
