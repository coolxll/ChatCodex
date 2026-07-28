"""Thread-free MCP gateway built on official FastMCP.

WebChat owns the conversation and model loop. Tools delegate standalone local
operations to ExecutionOrchestrator and all mutations to ApprovalBridge.
"""
from __future__ import annotations

import hashlib
import ipaddress
from typing import Any, Optional

from mcp import types as mtypes
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from . import widgets
from .approval import ApprovalBridge, ApprovalDeclined
from .config import Settings
from .execution import ExecutionError, ExecutionOrchestrator
from .oauth import Authenticator
from .tools import (
    WIDGET_ASK,
    WIDGET_CHAT,
    WIDGET_DIFF,
    WIDGET_WORKSPACE_SETUP,
)


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    """Keep no-auth MCP loopback-only; authenticated MCP may use tunnels."""
    if settings.mcp_auth_mode == "noauth":
        host = str(settings.host or "").strip().strip("[]").rstrip(".").lower()
        try:
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("MCP noauth mode may only bind to a loopback host")
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=[
                "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                "https://127.0.0.1:*", "https://localhost:*", "https://[::1]:*",
            ],
        )
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


class _Verifier(TokenVerifier):
    def __init__(self, auth: Authenticator):
        self.auth = auth

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        principal = self.auth.authenticate(f"Bearer {token}", "127.0.0.1")
        if not principal:
            return None
        return AccessToken(
            token=token,
            client_id=principal.client_id or principal.user_id,
            scopes=principal.scopes or ["codex"],
            expires_at=None,
        )


def tool_security_schemes(settings: Settings) -> list[dict[str, Any]]:
    if settings.mcp_auth_mode in ("oauth", "both"):
        return [{"type": "oauth2", "scopes": ["codex"]}]
    return [{"type": "noauth"}]


def _register_widget(mcp: FastMCP, settings: Settings, uri: str) -> None:
    description = widgets.WIDGETS[uri][1]

    @mcp.resource(
        uri,
        name=description,
        mime_type="text/html;profile=mcp-app",
        meta=widgets.resource_meta(description, settings.public_url),
    )
    def _read() -> str:
        return widgets.read_resource(settings, uri)["text"]


def update_widget_domains(mcp: FastMCP, public_url: str) -> None:
    for uri, resource in mcp._resource_manager._resources.items():  # noqa: SLF001
        description = widgets.WIDGETS.get(str(uri), ("", resource.name))[1]
        resource.meta = widgets.resource_meta(description, public_url)


def _meta(ctx: Context) -> dict:
    try:
        value = ctx.request_context.meta
        if hasattr(value, "model_dump"):
            return value.model_dump(by_alias=True, exclude_none=True)
        return dict(value) if value else {}
    except Exception:
        return {}


def request_identity(meta: dict) -> str:
    access = get_access_token()
    client_id = access.client_id if access else "local-noauth"
    subject = meta.get("openai/subject") or meta.get("openaiSubject") or ""
    conversation = (
        meta.get("openai/session")
        or meta.get("openaiConversationId")
        or meta.get("conversationId")
        or ""
    )
    return hashlib.sha256(
        f"{client_id}\0{subject}\0{conversation}".encode()
    ).hexdigest()[:32]


def _user(ctx: Context) -> str:
    return request_identity(_meta(ctx))


def _tool_result(data: dict[str, Any], summary: str) -> mtypes.CallToolResult:
    return mtypes.CallToolResult(
        content=[mtypes.TextContent(type="text", text=summary)],
        structuredContent=data,
    )


def _normalize_tool_contracts(mcp: FastMCP, settings: Settings) -> None:
    from mcp.types import ToolAnnotations

    schemas = _output_schemas()
    for name, tool in mcp._tool_manager._tools.items():  # noqa: SLF001
        if not tool.title:
            tool.title = name.replace("_", " ").title()
        old = tool.annotations or ToolAnnotations()
        read_only = bool(old.readOnlyHint) if old.readOnlyHint is not None else False
        tool.annotations = ToolAnnotations(
            title=old.title or tool.title,
            readOnlyHint=read_only,
            destructiveHint=(
                bool(old.destructiveHint)
                if old.destructiveHint is not None else not read_only
            ),
            idempotentHint=(
                bool(old.idempotentHint)
                if old.idempotentHint is not None else False
            ),
            openWorldHint=(
                bool(old.openWorldHint)
                if old.openWorldHint is not None else True
            ),
        )
        tool.meta = dict(tool.meta or {})
        tool.meta.setdefault("securitySchemes", tool_security_schemes(settings))
        if name in schemas:
            tool.output_schema = schemas[name]


def _output_schemas() -> dict[str, dict[str, Any]]:
    def obj(
            properties: dict[str, Any], required: Optional[list[str]] = None
    ) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required
        return schema

    string = {"type": "string"}
    boolean = {"type": "boolean"}
    integer = {"type": "integer"}
    any_object = {"type": "object", "additionalProperties": True}
    nullable_string = {"anyOf": [string, {"type": "null"}]}
    return {
        "open_workspace_setup": obj({
            "presented": boolean,
            "message": string,
            "suggestedCwd": string,
            "suggestedWorkMode": string,
            "defaults": any_object,
            "requirements": {"anyOf": [any_object, {"type": "null"}]},
        }, ["presented", "message"]),
        "save_execution_context": obj({
            "conversationId": string,
            "contextId": string,
            "contextVersion": integer,
            "cwd": string,
            "workspaceRoots": {"type": "array", "items": string},
            "sandbox": string,
            "permissionProfileId": nullable_string,
            "approvalPolicy": {},
            "approvalsReviewer": string,
            "workMode": string,
            "platform": string,
            "codexAgentSession": boolean,
            "capabilities": any_object,
        }, [
            "conversationId", "contextId", "contextVersion", "cwd",
            "workspaceRoots", "sandbox", "approvalPolicy",
            "approvalsReviewer", "workMode", "platform",
            "codexAgentSession",
            "capabilities",
        ]),
        "execution_status": obj({
            "conversationId": string,
            "contextId": string,
            "contextVersion": integer,
            "status": string,
            "pending": boolean,
            "approvals": {"type": "array", "items": any_object},
            "context": any_object,
            "capabilities": any_object,
        }, [
            "conversationId", "contextId", "contextVersion", "status",
            "pending", "approvals", "context", "capabilities",
        ]),
        "resolve_approval": obj({"resolved": boolean}, ["resolved"]),
        "read_file": obj({
            "path": string,
            "encoding": string,
            "sizeBytes": integer,
            "startLine": integer,
            "endLine": integer,
            "totalLines": integer,
            "content": string,
            "dataBase64": string,
            "truncated": boolean,
        }, ["path", "encoding", "sizeBytes", "truncated"]),
        "write_file": obj({
            "conversationId": string,
            "path": string,
            "encoding": string,
            "bytesWritten": integer,
            "written": boolean,
            "changed": boolean,
            "fileChanges": {"type": "array", "items": string},
            "diff": string,
            "diffTruncated": boolean,
        }, [
            "conversationId", "path", "encoding", "bytesWritten", "written",
            "changed", "fileChanges", "diff", "diffTruncated",
        ]),
        "list_dir": obj({
            "entries": {"type": "array", "items": any_object},
        }, ["entries"]),
        "search_files": obj({
            "files": {"type": "array", "items": any_object},
        }, ["files"]),
        "exec_command": obj({
            "exitCode": integer, "stdout": string, "stderr": string,
        }, ["exitCode", "stdout", "stderr"]),
        "apply_patch": obj({
            "conversationId": string,
            "applied": boolean,
            "fileChanges": {"type": "array", "items": string},
            "diff": string,
            "diffTruncated": boolean,
        }, ["conversationId", "applied", "fileChanges"]),
        "update_plan": obj({
            "conversationId": string,
            "updated": boolean,
            "explanation": string,
            "plan": {"type": "array", "items": any_object},
        }, ["conversationId", "updated", "explanation", "plan"]),
        "view_image": obj({
            "path": string, "mimeType": string, "sizeBytes": integer,
        }, ["path", "mimeType", "sizeBytes"]),
        "request_user_input": obj({
            "action": string,
            "questions": {"type": "array", "items": any_object},
        }, ["action", "questions"]),
        "browse_dir": obj({
            "path": string,
            "parent": nullable_string,
            "entries": {"type": "array", "items": any_object},
            "error": string,
        }, ["path", "entries"]),
    }


def build_mcp(
        settings: Settings,
        orch: ExecutionOrchestrator,
        approval: ApprovalBridge,
        auth: Optional[Authenticator] = None,
) -> FastMCP:
    auth_settings = None
    verifier = None
    if auth is not None and auth.mode != "noauth":
        auth_settings = AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=f"{settings.public_url.rstrip('/')}/mcp",
            required_scopes=["codex"],
        )
        verifier = _Verifier(auth)
    mcp = FastMCP(
        "chatcodex",
        stateless_http=True,
        json_response=True,
        transport_security=_transport_security(settings),
        auth=auth_settings,
        token_verifier=verifier,
        streamable_http_path="/",
    )

    def as_tool_error(exc: Exception) -> ToolError:
        if isinstance(exc, ExecutionError):
            hint = f". {exc.hint}" if exc.hint else ""
            return ToolError(f"{exc.code}: {exc}{hint}")
        if isinstance(exc, ApprovalDeclined):
            return ToolError(f"approval_declined: {exc}")
        return ToolError(str(exc))

    @mcp.tool(
        "open_workspace_setup",
        description=(
            "Open the WebChat execution-workspace picker. It configures cwd, "
            "workspace roots, sandbox, work mode, and approval policy without "
            "starting another model or agent session."
        ),
        meta={
            "ui": {"resourceUri": WIDGET_WORKSPACE_SETUP},
            "openai/outputTemplate": WIDGET_WORKSPACE_SETUP,
            "openai/toolInvocation/invoking": "Opening workspace setup",
            "openai/toolInvocation/invoked": "Workspace setup opened",
        },
        annotations={
            "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        },
    )
    async def open_workspace_setup(
            ctx: Context, cwd: Optional[str] = None, workMode: str = "agent"
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "presented": True,
            "message": "配置 WebChat 本地执行工作区；不会启动第二个模型会话。",
            "suggestedWorkMode": (
                workMode if workMode in {"agent", "plan"} else "agent"
            ),
            "defaults": {
                "sandbox": (
                    orch.store.get("sandbox") if orch.store
                    else "workspace-write"
                ),
                "approvalPolicy": (
                    orch.store.get("approval_policy") if orch.store
                    else "on-request"
                ),
                "codexAgentSession": False,
            },
        }
        if cwd:
            data["suggestedCwd"] = cwd
        try:
            requirements = await orch.appserver.call(
                "configRequirements/read", None
            )
            data["requirements"] = (
                requirements.get("requirements")
                if isinstance(requirements, dict) else None
            )
        except Exception:
            data["requirements"] = None
        return _tool_result(data, "Execution workspace setup opened.")

    @mcp.tool(
        "save_execution_context",
        description=(
            "Save the execution workspace and safety choices confirmed in the UI."
        ),
        meta={"ui": {"visibility": ["app"]}},
        annotations={
            "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": False, "openWorldHint": False,
        },
    )
    async def save_execution_context(
            ctx: Context, config: dict
    ) -> dict[str, Any]:
        try:
            data = await orch.configure_context(_user(ctx), _meta(ctx), config)
            try:
                await ctx.session.send_tool_list_changed()
            except Exception:
                pass
            return _tool_result(
                data, f"Execution workspace ready: {data['conversationId']}"
            )
        except Exception as exc:
            raise as_tool_error(exc) from exc

    @mcp.tool(
        "execution_status",
        description=(
            "Return the current WebChat execution context and pending approvals."
        ),
        meta={
            "ui": {"visibility": ["app"]},
            "openai/toolInvocation/invoking": "Checking operations",
            "openai/toolInvocation/invoked": "Operation status updated",
        },
        annotations={
            "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        },
    )
    async def execution_status(
            ctx: Context, conversationId: Optional[str] = None
    ) -> dict[str, Any]:
        try:
            result = await orch.context_status(_user(ctx), _meta(ctx))
            current = result["conversationId"]
            if conversationId and conversationId != current:
                raise ExecutionError(
                    "conversation_not_owned",
                    "execution context does not belong to this WebChat conversation",
                )
            result["approvals"] = approval.list_pending(current)
            result["pending"] = bool(result["approvals"])
            return _tool_result(
                result,
                f"Execution context ready; {len(result['approvals'])} approvals.",
            )
        except Exception as exc:
            raise as_tool_error(exc) from exc

    @mcp.tool(
        "resolve_approval",
        description="Resolve one approval owned by this WebChat conversation.",
        meta={"ui": {"visibility": ["app"]}},
        annotations={
            "readOnlyHint": False, "destructiveHint": True,
            "idempotentHint": True, "openWorldHint": False,
        },
    )
    async def resolve_approval(
            ctx: Context,
            requestId: str,
            action: Optional[str] = None,
            answers: Optional[dict] = None,
            content: Optional[dict] = None,
            permissions: Optional[dict] = None,
            scope: Optional[str] = None,
            expectedVersion: Optional[int] = None,
    ) -> dict[str, Any]:
        try:
            context = orch.active_context(_user(ctx), _meta(ctx))
            resolved = await approval.resolve(
                requestId,
                {
                    "action": action or "decline",
                    "answers": answers,
                    "content": content,
                    "permissions": permissions,
                    "scope": scope,
                },
                conversation_id=context.conversation_id,
                decided_by=_user(ctx),
                expected_version=expectedVersion,
            )
        except Exception as exc:
            raise as_tool_error(exc) from exc
        if not resolved:
            raise ToolError(
                "approval request is missing, expired, changed, or already resolved"
            )
        return {"resolved": True}

    async def run_mutation(
            ctx: Context,
            operation: str,
            payload: dict[str, Any],
            execute_factory,
    ) -> Any:
        user_id, meta = _user(ctx), _meta(ctx)
        try:
            context, envelope, decision = await orch.prepare_operation(
                user_id, meta, operation, payload
            )
            if decision.action == "forbid":
                raise ExecutionError("approval_forbidden", decision.reason)
            targets = list(decision.targets)
            if operation == "exec_command":
                command = envelope.arguments.get("command") or []
                cwd = envelope.arguments.get("cwd") or context.cwd
                command_timeout = envelope.arguments.get("timeoutMs")
                message = (
                    "ChatCodex needs one-time approval for an independent App "
                    "Server command.\n\n"
                    f"Command: {' '.join(command)[:2000]}\n"
                    f"Working directory: {cwd}\nReason: {decision.reason}"
                )
                kind = "commandExecution"
                params = {
                    "command": command,
                    "cwd": cwd,
                    "timeoutMs": command_timeout,
                    "reason": decision.reason,
                    "execPolicyMode": decision.execpolicy_mode,
                    "execPolicyDecision": decision.execpolicy_decision,
                    "matchedRules": decision.matched_rules,
                }
            else:
                verb = (
                    "write a file" if operation == "write_file"
                    else "apply a patch"
                )
                message = (
                    f"ChatCodex needs one-time approval to {verb}.\n\n"
                    + "\n".join(f"- {target}" for target in targets[:30])
                    + f"\nReason: {decision.reason}"
                )
                kind = "fileChange"
                params = {
                    "operation": operation,
                    "fileChanges": targets,
                    "reason": decision.reason,
                }
                if operation == "apply_patch":
                    patch_text = str(payload.get("patch") or "")
                    params["patchSha256"] = hashlib.sha256(
                        patch_text.encode()
                    ).hexdigest()
                    # Let the approval widget render the real patch diff while
                    # pending (the same text the ChatGPT host parses), bounded
                    # like the completion diff.
                    params["patch"] = patch_text[:200_000]
                else:
                    params["contentBytes"] = len(
                        str(payload.get("content") or "").encode("utf-8")
                    )

            async def execute():
                return await execute_factory(envelope)

            if decision.owner == "appserver":
                async with approval.native_operation(
                    conversation_id=context.conversation_id,
                    operation_id=envelope.operation_id,
                    user_id=user_id,
                ):
                    return await execute()
            if decision.action == "auto":
                return await execute()
            return await approval.run_gateway_operation(
                envelope=envelope,
                kind=kind,
                message=message,
                params=params,
                user_id=user_id,
                execute=execute,
            )
        except Exception as exc:
            raise as_tool_error(exc) from exc

    @mcp.tool(
        "read_file",
        description="Read a file inside the configured WebChat workspace.",
        meta={
            "openai/toolInvocation/invoking": "Reading file",
            "openai/toolInvocation/invoked": "Read file",
        },
        annotations={
            "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        },
    )
    async def read_file(
            ctx: Context,
            path: str,
            startLine: int = 1,
            endLine: Optional[int] = None,
            maxChars: int = 100_000,
    ) -> dict[str, Any]:
        try:
            return await orch.read_file(
                _user(ctx), _meta(ctx), path, startLine, endLine, maxChars
            )
        except Exception as exc:
            raise as_tool_error(exc) from exc

    @mcp.tool(
        "write_file",
        description=(
            "Write UTF-8 text inside the configured workspace after one-time "
            "Gateway approval."
        ),
        meta={
            "ui": {"resourceUri": WIDGET_DIFF},
            "openai/outputTemplate": WIDGET_DIFF,
            "openai/toolInvocation/invoking": "Awaiting file approval",
            "openai/toolInvocation/invoked": "File operation finished",
        },
        annotations={
            "readOnlyHint": False, "destructiveHint": True,
            "idempotentHint": True, "openWorldHint": False,
        },
    )
    async def write_file(
            ctx: Context, path: str, content: str
    ) -> dict[str, Any]:
        return await run_mutation(
            ctx,
            "write_file",
            {"path": path, "content": content},
            lambda envelope: orch.write_file(
                _user(ctx), _meta(ctx), path, content, envelope=envelope
            ),
        )

    @mcp.tool(
        "list_dir",
        description="List a directory inside the configured workspace.",
        meta={
            "openai/toolInvocation/invoking": "Listing directory",
            "openai/toolInvocation/invoked": "Listed directory",
        },
        annotations={
            "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        },
    )
    async def list_dir(
            ctx: Context, path: Optional[str] = None
    ) -> dict[str, Any]:
        try:
            return await orch.list_dir(_user(ctx), _meta(ctx), path or "")
        except Exception as exc:
            raise as_tool_error(exc) from exc

    @mcp.tool(
        "search_files",
        description="Fuzzy-search files inside the configured workspace.",
        meta={
            "openai/toolInvocation/invoking": "Searching files",
            "openai/toolInvocation/invoked": "Searched files",
        },
        annotations={
            "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        },
    )
    async def search_files(
            ctx: Context, query: str, path: Optional[str] = None
    ) -> dict[str, Any]:
        try:
            return await orch.search_files(
                _user(ctx), _meta(ctx), query, path or ""
            )
        except Exception as exc:
            raise as_tool_error(exc) from exc

    @mcp.tool(
        "exec_command",
        description=(
            "Execute a standalone argv command through official command/exec. "
            "Approval waiting does not consume timeoutMs."
        ),
        meta={
            "ui": {"resourceUri": WIDGET_CHAT},
            "openai/outputTemplate": WIDGET_CHAT,
            "openai/toolInvocation/invoking": "Awaiting command approval",
            "openai/toolInvocation/invoked": "Command finished",
        },
        annotations={
            "readOnlyHint": False, "destructiveHint": True,
            "idempotentHint": False, "openWorldHint": True,
        },
    )
    async def exec_command(
            ctx: Context,
            command: list[str],
            cwd: Optional[str] = None,
            timeoutMs: Optional[int] = None,
            requireEscalated: bool = False,
            justification: Optional[str] = None,
    ) -> dict[str, Any]:
        if (
            timeoutMs is not None
            and (
                isinstance(timeoutMs, bool)
                or timeoutMs < 0
            )
        ):
            raise ToolError("timeoutMs must be a non-negative integer")
        if requireEscalated and not str(justification or "").strip():
            raise ToolError(
                "justification is required when requireEscalated is true"
            )
        payload = {
            "command": command,
            "cwd": cwd or "",
            "timeoutMs": timeoutMs,
            "requireEscalated": requireEscalated,
            "justification": justification or "",
        }
        return await run_mutation(
            ctx,
            "exec_command",
            payload,
            lambda envelope: orch.exec(
                _user(ctx),
                _meta(ctx),
                command,
                cwd or "",
                timeoutMs,
                envelope=envelope,
                require_escalated=requireEscalated,
            ),
        )

    @mcp.tool(
        "apply_patch",
        description=(
            "Apply a Codex-format patch through official command/exec after "
            "one-time Gateway approval."
        ),
        meta={
            "ui": {"resourceUri": WIDGET_DIFF},
            "openai/outputTemplate": WIDGET_DIFF,
            "openai/toolInvocation/invoking": "Awaiting patch approval",
            "openai/toolInvocation/invoked": "Patch operation finished",
        },
        annotations={
            "readOnlyHint": False, "destructiveHint": True,
            "idempotentHint": False, "openWorldHint": False,
        },
    )
    async def apply_patch(ctx: Context, patch: str) -> dict[str, Any]:
        return await run_mutation(
            ctx,
            "apply_patch",
            {"patch": patch},
            lambda envelope: orch.apply_patch(
                _user(ctx), _meta(ctx), patch, envelope=envelope
            ),
        )

    @mcp.tool(
        "update_plan",
        description="Publish the WebChat coding plan without contacting Codex.",
        annotations={
            "readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        },
    )
    async def update_plan(
            ctx: Context,
            plan: list[dict],
            explanation: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            return await orch.update_plan(
                _user(ctx), _meta(ctx), plan, explanation or ""
            )
        except Exception as exc:
            raise as_tool_error(exc) from exc

    @mcp.tool(
        "view_image",
        description="Open a local image from the configured workspace.",
        annotations={
            "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        },
    )
    async def view_image(ctx: Context, path: str) -> mtypes.CallToolResult:
        try:
            data = await orch.view_image(_user(ctx), _meta(ctx), path)
        except Exception as exc:
            raise as_tool_error(exc) from exc
        return mtypes.CallToolResult(
            content=[
                mtypes.TextContent(
                    type="text", text=f"Opened image: {data['path']}"
                ),
                mtypes.ImageContent(
                    type="image",
                    data=data["dataBase64"],
                    mimeType=data["mimeType"],
                ),
            ],
            structuredContent={
                key: value for key, value in data.items()
                if key != "dataBase64"
            },
        )

    @mcp.tool(
        "request_user_input",
        description=(
            "Prepare one to three non-secret questions for WebChat to ask."
        ),
        meta={
            "ui": {"resourceUri": WIDGET_ASK},
            "openai/outputTemplate": WIDGET_ASK,
        },
        annotations={
            "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": False, "openWorldHint": False,
        },
    )
    async def request_user_input(
            ctx: Context, questions: list[dict]
    ) -> dict[str, Any]:
        if not 1 <= len(questions) <= 3:
            raise ToolError("questions must contain between one and three items")
        normalized = []
        seen = set()
        for index, question in enumerate(questions):
            if question.get("is_secret") or question.get("isSecret"):
                raise ToolError("request_user_input cannot collect secrets")
            question_id = str(
                question.get("id") or f"question_{index + 1}"
            )
            if (
                not question_id.isidentifier()
                or question_id.startswith("_")
                or question_id in seen
            ):
                raise ToolError(
                    f"invalid or duplicate question id: {question_id}"
                )
            seen.add(question_id)
            normalized.append({
                "id": question_id,
                "header": str(question.get("header") or ""),
                "question": str(
                    question.get("question")
                    or question.get("header")
                    or question_id
                ),
                "options": [
                    {
                        "label": str(
                            option.get("label") or option.get("value") or ""
                        ),
                        "description": str(option.get("description") or ""),
                    }
                    for option in (question.get("options") or [])
                    if str(option.get("label") or option.get("value") or "")
                ],
                "is_other": bool(
                    question.get("is_other") or question.get("isOther")
                ),
                "is_secret": False,
            })
        return {"action": "ask_user", "questions": normalized}

    @mcp.tool(
        "browse_dir",
        description="Browse server directories for workspace setup.",
        meta={"ui": {"visibility": ["app"]}},
        annotations={
            "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        },
    )
    async def browse_dir(path: Optional[str] = None) -> dict[str, Any]:
        return await orch.browse_dir(path or "")

    mcp._chatcodex_orch = orch  # noqa: SLF001
    mcp._chatcodex_approval = approval  # noqa: SLF001
    for uri in widgets.WIDGETS:
        _register_widget(mcp, settings, uri)
    _normalize_tool_contracts(mcp, settings)
    return mcp
