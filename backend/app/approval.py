"""Unified approval coordinator for native reverse RPCs and Gateway fallback."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .db import Database
from .events import EventBroker


KIND_BY_METHOD = {
    "item/commandExecution/requestApproval": "commandExecution",
    "item/fileChange/requestApproval": "fileChange",
    "item/permissions/requestApproval": "permissions",
    "item/tool/requestUserInput": "userInput",
    "mcpServer/elicitation/request": "elicitation",
}
DEFAULT_TIMEOUT = 300.0
APPROVED_ACTIONS = {"accept", "approve", "approve_once", "allow"}
PendingHook = Callable[[dict], Awaitable[None]]


class ApprovalDeclined(RuntimeError):
    pass


@dataclass
class PendingRequest:
    request_id: str
    method: str
    kind: str
    params: dict = field(default_factory=dict)
    conversation_id: str = ""
    operation_id: str = ""
    source: str = "appserver"
    owner: str = "native"
    state: str = "pending"
    available_decisions: tuple[str, ...] = ()
    action_digest: str = ""
    context_version: int = 0
    upstream_request_id: Optional[str] = None
    decided_by: Optional[str] = None
    audit_id: str = field(default_factory=lambda: secrets.token_hex(12))
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    version: int = 1
    fut: asyncio.Future = field(default_factory=asyncio.Future)

    def __post_init__(self) -> None:
        if not self.available_decisions:
            self.available_decisions = (
                ("approve_once", "decline", "cancel")
                if self.source == "gateway"
                else ("accept", "decline", "cancel")
            )
        if not self.expires_at:
            self.expires_at = self.created_at + DEFAULT_TIMEOUT

    def public(self) -> dict[str, Any]:
        presentation = {
            "message": str(self.params.get("message") or ""),
            "details": self.params,
        }
        return {
            "requestId": self.request_id,
            "method": self.method,
            "kind": self.kind,
            "source": self.source,
            "owner": self.owner,
            "conversationId": self.conversation_id,
            "operationId": self.operation_id,
            "state": self.state,
            "presentation": presentation,
            "params": self.params,
            "availableDecisions": list(self.available_decisions),
            "actionDigest": self.action_digest,
            "contextVersion": self.context_version,
            "upstreamRequestId": self.upstream_request_id,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "version": self.version,
        }


class ApprovalBridge:
    """One pending store, decision API, audit log and event source."""

    def __init__(
            self, appserver: Any, db: Database,
            on_pending: Optional[PendingHook] = None,
            events: Optional[EventBroker] = None,
            approval_timeout_ms: int = int(DEFAULT_TIMEOUT * 1000),
    ):
        self.appserver = appserver
        self.db = db
        self.on_pending = on_pending
        self.events = events
        self.default_timeout = max(
            1.0, float(approval_timeout_ms) / 1000.0
        )
        self._pending: dict[str, PendingRequest] = {}
        self._operation_futures: dict[str, asyncio.Future] = {}
        self._native_lane = asyncio.Lock()
        self._native_context: Optional[dict[str, str]] = None

    def list_pending(
            self, conversation_id: Optional[str] = None
    ) -> list[dict]:
        wanted = conversation_id or ""
        out = []
        for pending in self._pending.values():
            if wanted and pending.conversation_id != wanted:
                continue
            if pending.state == "pending":
                out.append(pending.public())
        return sorted(out, key=lambda item: item["createdAt"])

    def owns_request(self, request_id: str, conversation_id: str) -> bool:
        pending = self._pending.get(request_id)
        return bool(
            pending and pending.conversation_id == conversation_id
            and pending.state == "pending"
        )

    def cancel_pending(
            self, reason: str = "appserver_reset",
            conversation_id: str = "",
    ) -> int:
        cancelled = 0
        for pending in list(self._pending.values()):
            if (
                pending.fut.done() or pending.state != "pending"
                or (
                    conversation_id
                    and pending.conversation_id != conversation_id
                )
            ):
                continue
            pending.decided_by = "gateway"
            pending.state = "cancelled"
            pending.version += 1
            decision = {"action": "cancel", "source": reason}
            pending.fut.set_result(decision)
            self._audit(pending, decision)
            self._schedule_event("approval.resolved", pending)
            cancelled += 1
        return cancelled

    @asynccontextmanager
    async def native_operation(
            self, *, conversation_id: str, operation_id: str, user_id: str
    ):
        """Serialize future standalone native-approval RPCs without threads."""
        async with self._native_lane:
            self._native_context = {
                "conversationId": conversation_id,
                "operationId": operation_id,
                "userId": user_id,
            }
            try:
                yield
            finally:
                self._native_context = None

    async def handle(self, msg: dict) -> dict:
        """Handle a correlated official App Server reverse request."""
        method = str(msg.get("method") or "")
        if method not in KIND_BY_METHOD:
            raise RuntimeError(
                f"unsupported Codex App Server request: {method or '<missing>'}"
            )
        params = msg.get("params") or {}
        active = self._native_context
        supplied_conversation = str(params.get("conversationId") or "")
        supplied_operation = str(params.get("operationId") or "")
        correlated = bool(active)
        if active and supplied_conversation:
            correlated = supplied_conversation == active["conversationId"]
        if active and supplied_operation:
            correlated = (
                correlated and supplied_operation == active["operationId"]
            )
        conversation_id = active["conversationId"] if active else ""
        operation_id = active["operationId"] if active else ""
        # A threadId is intentionally not accepted as ownership evidence. This
        # prevents an obsolete Codex turn from leaking an approval to WebChat.
        if not correlated or not conversation_id or not operation_id:
            rejected = PendingRequest(
                request_id=f"native-{secrets.token_hex(12)}",
                method=method,
                kind=KIND_BY_METHOD[method],
                source="appserver",
                owner="native",
                state="cancelled",
                params=_redact_params(params),
            )
            self._audit(rejected, None)
            self._audit(rejected, {
                "action": "cancel",
                "source": "uncorrelated_reverse_request",
            })
            return self._to_response(method, {"action": "cancel"})

        upstream = (
            params.get("approvalId") or params.get("callId") or params.get("itemId")
        )
        timeout = self.default_timeout
        auto_ms = params.get("autoResolutionMs")
        if isinstance(auto_ms, (int, float)) and auto_ms > 0:
            timeout = min(timeout, float(auto_ms) / 1000.0)
        pending = PendingRequest(
            request_id=f"native-{secrets.token_hex(12)}",
            method=method,
            kind=KIND_BY_METHOD[method],
            conversation_id=conversation_id,
            operation_id=operation_id,
            source="appserver",
            owner="native",
            params={
                **params,
                "conversationId": conversation_id,
                "operationId": operation_id,
            },
            upstream_request_id=str(upstream) if upstream is not None else None,
            decided_by=active.get("userId") if active else None,
            available_decisions=tuple(
                str(value) for value in (params.get("availableDecisions") or ())
                if str(value)
            ),
            expires_at=time.time() + timeout,
        )
        await self._register(pending)
        decision = await self._wait_for_decision(pending, timeout)
        await self._finish_pending(pending, decision)
        return self._to_response(method, decision)

    async def run_gateway_operation(
            self, *, envelope: Any, kind: str, message: str, params: dict,
            user_id: str, execute: Callable[[], Awaitable[Any]],
            validate: Optional[Callable[[], Any]] = None,
            timeout: Optional[float] = None,
    ) -> Any:
        """Wait for one visible approval, then execute the immutable action once."""
        timeout = self.default_timeout if timeout is None else timeout
        existing = self._operation_futures.get(envelope.operation_id)
        if existing is not None:
            return await asyncio.shield(existing)
        result_future = asyncio.get_running_loop().create_future()
        self._operation_futures[envelope.operation_id] = result_future
        pending = PendingRequest(
            request_id=f"gateway-{secrets.token_hex(12)}",
            method=f"gateway/{kind}/requestApproval",
            kind=kind,
            conversation_id=envelope.conversation_id,
            operation_id=envelope.operation_id,
            source="gateway",
            owner="fallback",
            params={
                "conversationId": envelope.conversation_id,
                "operationId": envelope.operation_id,
                "message": message,
                **params,
            },
            decided_by=user_id,
            action_digest=envelope.action_digest,
            context_version=envelope.context_version,
            expires_at=time.time() + timeout,
        )
        try:
            await self._register(pending)
            decision = await self._wait_for_decision(pending, timeout)
            if str(decision.get("action") or "") not in APPROVED_ACTIONS:
                await self._finish_pending(pending, decision)
                raise ApprovalDeclined(
                    "the user declined, cancelled, or did not answer this local action"
                )
            await self._finish_pending(pending, decision)
            try:
                if validate is not None:
                    validated = validate()
                    if asyncio.iscoroutine(validated):
                        await validated
                pending.state = "executing"
                pending.version += 1
                await self._emit("operation.executing", pending)
                result = await execute()
            except Exception as exc:
                pending.state = "failed"
                pending.version += 1
                await self._emit(
                    "operation.failed", pending, {"error": str(exc)[:500]}
                )
                self._audit(pending, {
                    "action": "failed", "error": str(exc)[:500],
                })
                if not result_future.done():
                    result_future.set_exception(exc)
                    # The first caller raises directly; consume the stored
                    # future exception so loop shutdown has no noisy warning.
                    result_future.exception()
                raise
            pending.state = "completed"
            pending.version += 1
            await self._emit("operation.completed", pending)
            self._audit(pending, {"action": "completed", "source": "gateway"})
            if not result_future.done():
                result_future.set_result(result)
            return result
        except Exception as exc:
            if not result_future.done():
                result_future.set_exception(exc)
                result_future.exception()
            raise
        finally:
            self._pending.pop(pending.request_id, None)
            # Keep the completed future for the lifetime of this process so an
            # accidental retry with the same operation id cannot execute twice.

    async def resolve(
            self, request_id: str, decision: dict,
            conversation_id: Optional[str] = None,
            decided_by: Optional[str] = None,
            expected_version: Optional[int] = None,
    ) -> bool:
        pending = self._pending.get(request_id)
        if (
            not pending or pending.fut.done() or pending.state != "pending"
            or (
                conversation_id is not None
                and pending.conversation_id != conversation_id
            )
            or (expected_version is not None and pending.version != expected_version)
        ):
            return False
        action = str((decision or {}).get("action") or "")
        if action not in pending.available_decisions:
            return False
        pending.decided_by = decided_by
        pending.state = (
            "approved" if action in APPROVED_ACTIONS
            else "cancelled" if action == "cancel"
            else "declined"
        )
        pending.version += 1
        pending.fut.set_result(decision)
        await self._emit("approval.updated", pending, {
            "decision": _redact_decision(decision),
        })
        return True

    async def _register(self, pending: PendingRequest) -> None:
        if pending.request_id in self._pending:
            raise RuntimeError("duplicate approval request id")
        self._pending[pending.request_id] = pending
        self._audit(pending, None)
        await self._emit("approval.created", pending)
        if self.on_pending:
            try:
                await self.on_pending(pending.public())
            except Exception:
                pass

    async def _wait_for_decision(
            self, pending: PendingRequest, timeout: float
    ) -> dict:
        try:
            return await asyncio.wait_for(pending.fut, timeout)
        except asyncio.TimeoutError:
            return {"action": "timeout", "source": "gateway"}

    async def _finish_pending(
            self, pending: PendingRequest, decision: dict
    ) -> None:
        action = str((decision or {}).get("action") or "decline")
        if action in APPROVED_ACTIONS:
            pending.state = "approved"
        elif action == "timeout":
            pending.state = "expired"
        elif action == "cancel":
            pending.state = "cancelled"
        else:
            pending.state = "declined"
        pending.version += 1
        self._audit(pending, decision)
        await self._emit("approval.resolved", pending, {
            "decision": _redact_decision(decision),
        })
        self._pending.pop(pending.request_id, None)

    async def _emit(
            self, event: str, pending: PendingRequest,
            extra: Optional[dict] = None,
    ) -> None:
        if self.events is None:
            return
        await self.events.publish(
            event, pending.conversation_id,
            {"approval": pending.public(), **(extra or {})},
        )

    def _schedule_event(
            self, event: str, pending: PendingRequest,
            extra: Optional[dict] = None,
    ) -> None:
        if self.events is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self._emit(event, pending, extra)
            )
            task.add_done_callback(
                lambda done: None if done.cancelled() else done.exception()
            )
        except RuntimeError:
            pass

    @staticmethod
    def _to_response(method: str, decision: dict) -> dict:
        action = (decision or {}).get("action", "decline")
        values = {
            "accept": "accept", "approve": "accept", "approve_once": "accept",
            "allow": "accept", "always": "acceptForSession",
            "acceptForSession": "acceptForSession", "decline": "decline",
            "deny": "decline", "reject": "decline", "timeout": "decline",
            "cancel": "cancel", "abort": "cancel",
        }
        value = values.get(action, "decline")
        if method in {
            "item/fileChange/requestApproval",
            "item/commandExecution/requestApproval",
        }:
            return {"decision": value}
        if method == "item/permissions/requestApproval":
            permissions = (
                decision.get("permissions")
                if value in {"accept", "acceptForSession"} else {}
            )
            return {
                "permissions": permissions or {},
                "scope": decision.get("scope") or (
                    "session" if value == "acceptForSession" else "turn"
                ),
                **(
                    {"strictAutoReview": bool(decision["strictAutoReview"])}
                    if "strictAutoReview" in decision else {}
                ),
            }
        if method == "item/tool/requestUserInput":
            if action in {"cancel", "timeout", "decline"}:
                return {"answers": {}}
            answers = {}
            for key, answer in (decision.get("answers") or {}).items():
                if isinstance(answer, dict) and isinstance(answer.get("answers"), list):
                    answers[key] = answer
                elif isinstance(answer, list):
                    answers[key] = {"answers": [str(value) for value in answer]}
                else:
                    answers[key] = {"answers": [str(answer)]}
            return {"answers": answers}
        if method == "mcpServer/elicitation/request":
            mapped = {
                "accept": "accept", "approve": "accept",
                "approve_once": "accept", "decline": "decline",
                "deny": "decline", "cancel": "cancel", "timeout": "cancel",
            }
            response: dict[str, Any] = {
                "action": mapped.get(str(action), "decline")
            }
            if decision.get("content") is not None:
                response["content"] = decision["content"]
            if decision.get("_meta") is not None:
                response["_meta"] = decision["_meta"]
            return response
        return decision or {}

    def _audit(self, pending: PendingRequest, decision: Optional[dict]) -> None:
        try:
            with self.db.conn() as connection:
                if decision is None:
                    connection.execute(
                        """INSERT OR REPLACE INTO approval_audit
                           (id,conversation_id,operation_id,source,state,kind,
                            request_id,summary,payload,decision,decided_by,
                            action_digest,context_version,request_version,
                            created_at,decided_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            pending.audit_id, pending.conversation_id,
                            pending.operation_id, pending.source, pending.state,
                            pending.kind,
                            pending.request_id, _summarize(pending),
                            json.dumps(
                                _redact_params(pending.params),
                                ensure_ascii=False,
                            ),
                            None, None, pending.action_digest,
                            pending.context_version, pending.version,
                            int(pending.created_at), None,
                        ),
                    )
                else:
                    connection.execute(
                        """UPDATE approval_audit
                           SET decision=?,decided_by=?,decided_at=?,state=?,
                               request_version=? WHERE id=?""",
                        (
                            json.dumps(
                                _redact_decision(decision), ensure_ascii=False
                            ),
                            pending.decided_by, int(time.time()), pending.state,
                            pending.version, pending.audit_id,
                        ),
                    )
        except Exception:
            pass


def _summarize(pending: PendingRequest) -> str:
    params = pending.params
    if pending.kind == "commandExecution":
        command = params.get("command")
        if isinstance(command, list) and command:
            return f"cmd: {command[0]} ({max(0, len(command) - 1)} args)"[:200]
        return "command execution"
    if pending.kind == "fileChange":
        return (
            "write access: "
            + str(params.get("grantRoot") or params.get("reason") or "workspace")
        )[:200]
    return str(
        params.get("reason") or params.get("message") or pending.method
    )[:200]


def _redact_params(params: dict) -> dict:
    params = params or {}
    safe: dict[str, Any] = {}
    for key in (
        "conversationId", "operationId", "requestId", "approvalId", "callId",
        "itemId", "reason", "cwd", "grantRoot", "operation", "patchSha256",
        "contentBytes", "mode", "elicitationId", "availableDecisions",
    ):
        if params.get(key) is not None:
            safe[key] = params[key]
    command = params.get("command")
    if isinstance(command, list):
        safe["command"] = {
            "executable": str(command[0]) if command else "",
            "argumentCount": max(0, len(command) - 1),
        }
    file_changes = params.get("fileChanges")
    if isinstance(file_changes, dict):
        safe["fileChanges"] = sorted(str(path) for path in file_changes)
    elif isinstance(file_changes, list):
        safe["fileChanges"] = [str(path) for path in file_changes]
    permissions = params.get("permissions")
    if isinstance(permissions, dict):
        safe["permissionFields"] = sorted(str(name) for name in permissions)
    schema = params.get("requestedSchema")
    if isinstance(schema, dict):
        properties = schema.get("properties") or {}
        safe["requestedFields"] = (
            sorted(str(name) for name in properties)
            if isinstance(properties, dict) else []
        )
    return safe


def _redact_decision(decision: Optional[dict]) -> dict:
    safe = {
        key: value for key, value in (decision or {}).items()
        if key in {"action", "source", "error"}
    }
    for key in ("answers", "content", "permissions"):
        value = (decision or {}).get(key)
        if isinstance(value, dict):
            safe[f"{key}Fields"] = sorted(str(name) for name in value)
        elif value is not None:
            safe[f"has{key[:1].upper()}{key[1:]}"] = True
    return safe
