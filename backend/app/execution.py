"""Thread-free WebChat execution context and standalone App Server operations."""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import mimetypes
import ntpath
import os
import platform as host_platform
import posixpath
import re
from typing import Any, Optional

from .config import Settings
from .appserver.mcp_carrier import McpCarrier
from .models import ExecutionContext, ExecutionRegistry
from .operations import OperationEnvelope, OperationRouter, PolicyDecision


MAX_WIDGET_DIFF_CHARS = 200_000
# Forwarded MCP tool calls may run longer than standalone fs RPCs; cap them so a
# hung downstream server cannot hold an approval slot forever.
MCP_TOOL_TIMEOUT_MS = 120_000
MCP_TOOL_TIMEOUT_SEC = float(MCP_TOOL_TIMEOUT_MS) / 1000.0

# Gateway-side policy for which downstream MCP tools WebChat may see and call.
# Unlisted tools default to "deny".  "allow" runs directly (read-only only);
# anything with side effects always falls back to a one-time Gateway approval.
MCP_TOOL_POLICY_DEFAULT = "deny"
MCP_TOOL_POLICIES = {"allow", "ask", "deny"}


class ExecutionError(Exception):
    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


def conversation_id(meta: dict, user_id: str) -> str:
    value = (
        (meta or {}).get("openai/session")
        or (meta or {}).get("openaiConversationId")
        or (meta or {}).get("conversationId")
    )
    if value:
        return str(value)
    token = (
        (meta or {}).get("openai/subject")
        or (meta or {}).get("openaiSubject")
        or (meta or {}).get("userToken")
        or user_id
    )
    return "conv-" + hashlib.sha256(
        f"{user_id}\0{token}".encode()
    ).hexdigest()[:24]


def make_map_key(meta: dict, user_id: str) -> str:
    return hashlib.sha256(
        f"user:{user_id}:conv:{conversation_id(meta, user_id)}".encode()
    ).hexdigest()[:32]


def _is_missing_file_error(exc: Exception) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in (
        "no such file", "cannot find the file", "cannot find the path",
        "os error 2", "找不到文件", "找不到指定的路径",
    ))


def _write_file_diff(
        path: str, before: bytes, after: bytes, existed: bool
) -> tuple[str, bool]:
    if existed and before == after:
        return "", False
    old_name = f"a/{path}"
    new_name = f"b/{path}"
    header = [f"diff --git {old_name} {new_name}"]
    if not existed:
        header.append("new file mode 100644")
    try:
        before_text = before.decode("utf-8")
        after_text = after.decode("utf-8")
    except UnicodeDecodeError:
        diff = "\n".join([
            *header, f"Binary files {old_name} and {new_name} differ", "",
        ])
    else:
        body = difflib.unified_diff(
            before_text.splitlines(), after_text.splitlines(),
            fromfile=old_name if existed else "/dev/null",
            tofile=new_name, lineterm="",
        )
        diff = "\n".join([*header, *body, ""])
    if len(diff) <= MAX_WIDGET_DIFF_CHARS:
        return diff, False
    suffix = "\n… Diff truncated for display …\n"
    return diff[:MAX_WIDGET_DIFF_CHARS - len(suffix)] + suffix, True


class ExecutionOrchestrator:
    """WebChat owns the conversation; this class owns only local execution."""

    def __init__(
            self, settings: Settings, appserver: Any,
            registry: ExecutionRegistry, store: Any = None,
    ):
        self.settings = settings
        self.appserver = appserver
        self.registry = registry
        self.store = store
        self.router = OperationRouter(appserver, settings)
        self._plans: dict[str, dict[str, Any]] = {}
        self._carrier = McpCarrier(appserver)

    async def configure_context(
            self, user_id: str, meta: dict, config: dict
    ) -> dict[str, Any]:
        cwd_raw = str(config.get("cwd") or "").strip()
        if not cwd_raw:
            raise ExecutionError(
                "invalid_workspace", "choose an existing workspace directory"
            )
        status = self.appserver.status() or {}
        external = self._is_external()
        target_platform = str(status.get("platformOs") or "").strip().lower()
        if external and not target_platform:
            raise ExecutionError(
                "appserver_platform_unavailable",
                "the external App Server did not report its target platform",
            )
        target_platform = target_platform or host_platform.system().lower()
        if external:
            cwd = _normalize_target_path(
                cwd_raw, target_platform, require_absolute=True
            )
        else:
            cwd = os.path.realpath(os.path.abspath(os.path.expanduser(cwd_raw)))
            if not os.path.isdir(cwd):
                raise ExecutionError(
                    "invalid_workspace", "choose an existing workspace directory"
                )
        roots_raw = config.get("workspaceRoots") or [cwd]
        roots: list[str] = []
        for raw in roots_raw:
            if external:
                root = _normalize_target_path(
                    str(raw), target_platform, require_absolute=True
                )
            else:
                root = os.path.realpath(
                    os.path.abspath(os.path.expanduser(str(raw)))
                )
                if not os.path.isdir(root):
                    raise ExecutionError(
                        "invalid_workspace_root",
                        f"workspace root does not exist: {root}",
                    )
            if root not in roots:
                roots.append(root)
        if not any(
            _inside_target(cwd, root, target_platform, canonical=not external)
            for root in roots
        ):
            raise ExecutionError(
                "invalid_workspace", "working directory must be inside a workspace root"
            )

        work_mode = str(
            config.get("workMode")
            or (self.store.get("work_mode") if self.store else "agent")
            or "agent"
        )
        if work_mode not in {"agent", "plan"}:
            raise ExecutionError(
                "invalid_work_mode", f"unsupported collaboration mode: {work_mode}"
            )
        default_sandbox = "read-only" if work_mode == "plan" else "workspace-write"
        sandbox_mode = str(
            config.get("sandbox")
            or (self.store.get("sandbox") if self.store else "")
            or default_sandbox
        )
        if sandbox_mode not in {
            "read-only", "workspace-write", "danger-full-access",
        }:
            raise ExecutionError(
                "invalid_sandbox", f"unsupported sandbox mode: {sandbox_mode}"
            )
        approval = config.get("approvalPolicy")
        if approval is None and self.store:
            approval = self.store.get("approval_policy")
        approval = approval or ("untrusted" if work_mode == "plan" else "on-request")
        if isinstance(approval, dict):
            approval_text = json.dumps(approval, sort_keys=True)
        else:
            approval_text = str(approval)
            if approval_text not in {"untrusted", "on-request", "never"}:
                raise ExecutionError(
                    "invalid_approval_policy",
                    f"unsupported approval policy: {approval_text}",
                )
        permission_profile = config.get("permissionProfileId")
        appserver_instance = str(
            status.get("instanceId")
            or status.get("pid")
            or ""
        )
        context = self.registry.configure(
            map_key=make_map_key(meta, user_id),
            conversation_id=conversation_id(meta, user_id),
            user_id=user_id,
            cwd=cwd,
            workspace_roots=roots,
            sandbox_mode=sandbox_mode,
            permission_profile_id=(
                str(permission_profile) if permission_profile else None
            ),
            approval_policy=approval_text,
            work_mode=work_mode,
            platform_name=target_platform,
            appserver_instance_id=appserver_instance,
        )
        # The workspace/security version changed; drop any carrier thread so the
        # next MCP call re-creates it against the fresh context.
        await self._carrier.drop(context.id)
        return {
            "conversationId": context.conversation_id,
            "contextId": context.id,
            "contextVersion": context.version,
            "cwd": context.cwd,
            "workspaceRoots": context.roots(),
            "sandbox": context.sandbox_mode,
            "permissionProfileId": context.permission_profile_id,
            "approvalPolicy": (
                approval if isinstance(approval, dict) else approval_text
            ),
            "approvalsReviewer": "user",
            "workMode": context.work_mode,
            "platform": context.platform,
            "codexAgentSession": False,
            "capabilities": self.router.capabilities(),
        }

    def active_context(self, user_id: str, meta: dict) -> ExecutionContext:
        context = self.registry.get_by_map_key(make_map_key(meta, user_id))
        if not context or context.status != "active":
            raise ExecutionError(
                "no_execution_context",
                "no active WebChat execution workspace",
                hint="open workspace setup and choose a directory first",
            )
        status = self.appserver.status() or {}
        current_instance = str(status.get("instanceId") or "")
        if (
            current_instance
            and context.appserver_instance_id != current_instance
        ):
            raise ExecutionError(
                "stale_execution_context",
                "the App Server instance changed after this workspace was configured",
                hint="review and save the execution workspace again",
            )
        return context

    # Compatibility alias for thin tool wrappers.
    active_session = active_context

    @staticmethod
    def action_context_token(context: ExecutionContext) -> str:
        fields = (
            context.id, context.conversation_id, context.cwd,
            context.workspace_roots, context.sandbox_mode,
            context.permission_profile_id or "", context.approval_policy,
            context.approvals_reviewer, context.work_mode,
            context.platform, context.appserver_instance_id,
            str(context.version),
        )
        return hashlib.sha256("\0".join(fields).encode()).hexdigest()

    action_session_token = action_context_token

    async def validate_operation(
            self, user_id: str, meta: dict, envelope: OperationEnvelope
    ) -> ExecutionContext:
        current = self.active_context(user_id, meta)
        requirements_fingerprint = (
            await self._managed_requirements_fingerprint(current)
        )
        policy_fingerprint = ""
        if envelope.method == "exec_command":
            operation_cwd = str(
                envelope.arguments.get("cwd") or current.cwd
            )
            probe = await self.router.execpolicy.evaluate(
                list(envelope.arguments.get("command") or []), operation_cwd
            )
            if probe.error:
                raise ExecutionError(
                    "execution_policy_changed",
                    f"exec-policy could not be revalidated: {probe.error}",
                )
            if probe.decision == "forbidden":
                raise ExecutionError(
                    "execution_policy_changed",
                    "an exec-policy rule now forbids this command",
                )
            policy_fingerprint = probe.fingerprint
        expected_context = {
            "cwd": current.cwd,
            "workspaceRoots": current.roots(),
            "sandboxMode": current.sandbox_mode,
            "permissionProfileId": current.permission_profile_id,
            "approvalPolicy": current.approval_policy,
            "workMode": current.work_mode,
            "platform": current.platform,
            "appServerInstanceId": current.appserver_instance_id,
            "execPolicyFingerprint": policy_fingerprint,
            "requirementsFingerprint": requirements_fingerprint,
        }
        expected_digest = self.router.envelope(
            current, user_id, envelope.method, envelope.arguments,
            policy_fingerprint=policy_fingerprint,
            requirements_fingerprint=requirements_fingerprint,
        ).action_digest
        if (
            envelope.user_id != user_id
            or current.conversation_id != envelope.conversation_id
            or current.version != envelope.context_version
            or envelope.execution_context != expected_context
            or envelope.action_digest != expected_digest
            or envelope.approval_owner != self.router.owner_for(envelope.method)
        ):
            raise ExecutionError(
                "execution_context_changed",
                "workspace or permission configuration changed while approval was pending",
                hint="review the current workspace and call the tool again",
            )
        return current

    async def prepare_operation(
            self, user_id: str, meta: dict, operation: str, payload: dict
    ) -> tuple[ExecutionContext, OperationEnvelope, PolicyDecision]:
        context = self.active_context(user_id, meta)
        requirements_fingerprint = (
            await self._managed_requirements_fingerprint(context)
        )
        targets: tuple[str, ...] = ()
        normalized = dict(payload)
        if operation == "exec_command":
            command = normalized.get("command") or []
            if not command or not all(
                isinstance(part, str) and part for part in command
            ):
                raise ExecutionError(
                    "invalid_command", "command must be a non-empty argv array"
                )
            normalized["cwd"] = self._resolve(
                context, str(normalized.get("cwd") or context.cwd)
            )
        elif operation == "write_file":
            self._assert_standalone_fs_available()
            target = self._resolve(context, str(normalized.get("path") or ""))
            targets = (target,)
        elif operation == "apply_patch":
            targets = tuple(
                self._validate_patch_paths(
                    context, str(normalized.get("patch") or "")
                )
            )
        probe = None
        if operation == "exec_command":
            probe = await self.router.execpolicy.evaluate(
                list(normalized.get("command") or []),
                str(normalized.get("cwd") or context.cwd),
            )
        elif operation == "mcp/tool/call":
            server = str(normalized.get("server") or "")
            tool = str(normalized.get("tool") or "")
            if self._mcp_tool_policy(server, tool) == "deny":
                raise ExecutionError(
                    "mcp_tool_forbidden",
                    f"MCP tool is not exposed to WebChat: {server}/{tool}",
                )
            targets = (f"{server}/{tool}",)
        envelope = self.router.envelope(
            context, user_id, operation, normalized,
            policy_fingerprint=probe.fingerprint if probe else "",
            requirements_fingerprint=requirements_fingerprint,
        )
        decision = await self.router.decide(
            context, envelope, targets, execpolicy_result=probe
        )
        return context, envelope, decision

    async def _managed_requirements_fingerprint(
            self, context: ExecutionContext
    ) -> str:
        try:
            response = await self.appserver.call(
                "configRequirements/read", None, timeout=8.0
            )
        except Exception as exc:
            raise ExecutionError(
                "policy_state_unavailable",
                f"managed App Server requirements could not be read: {exc}",
            ) from exc
        if not isinstance(response, dict):
            raise ExecutionError(
                "policy_state_unavailable",
                "managed App Server requirements returned an invalid response",
            )
        requirements = response.get("requirements")
        if requirements is not None and not isinstance(requirements, dict):
            raise ExecutionError(
                "policy_state_unavailable",
                "managed App Server requirements returned an invalid policy",
            )
        self._assert_managed_requirements(context, requirements or {})
        encoded = json.dumps(
            response,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _assert_managed_requirements(
            context: ExecutionContext, requirements: dict[str, Any]
    ) -> None:
        sandboxes = requirements.get("allowedSandboxModes")
        if (
            isinstance(sandboxes, list)
            and context.sandbox_mode not in sandboxes
        ):
            raise ExecutionError(
                "managed_policy_forbidden",
                "the configured sandbox mode is disallowed by App Server requirements",
            )

        policies = requirements.get("allowedApprovalPolicies")
        if isinstance(policies, list):
            try:
                selected: Any = json.loads(context.approval_policy)
            except json.JSONDecodeError:
                selected = context.approval_policy
            selected_key = json.dumps(
                selected, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False,
            )
            allowed_keys = {
                json.dumps(
                    value, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False,
                )
                for value in policies
            }
            if selected_key not in allowed_keys:
                raise ExecutionError(
                    "managed_policy_forbidden",
                    "the configured approval policy is disallowed by App Server requirements",
                )

        reviewers = requirements.get("allowedApprovalsReviewers")
        if isinstance(reviewers, list) and "user" not in reviewers:
            raise ExecutionError(
                "managed_policy_forbidden",
                "App Server requirements do not permit user-reviewed approvals",
            )

        profiles = requirements.get("allowedPermissionProfiles")
        if (
            context.permission_profile_id
            and isinstance(profiles, dict)
            and not bool(profiles.get(context.permission_profile_id))
        ):
            raise ExecutionError(
                "managed_policy_forbidden",
                "the selected permission profile is disallowed by App Server requirements",
            )

    def _is_external(self) -> bool:
        return str(
            getattr(self.settings, "codex_app_mode", "internal")
        ).lower() == "external"

    def _assert_standalone_fs_available(self) -> None:
        if self._is_external():
            raise ExecutionError(
                "remote_filesystem_boundary_unavailable",
                (
                    "standalone filesystem RPCs are disabled for an external "
                    "App Server because the protocol cannot prove canonical "
                    "workspace boundaries"
                ),
                hint=(
                    "use an internal App Server, or use an approved sandboxed "
                    "command on the external host"
                ),
            )

    def _resolve(self, context: ExecutionContext, path: str) -> str:
        if not path:
            raise ExecutionError("invalid_path", "path is required")
        if self._is_external():
            resolved = _normalize_target_path(
                path, context.platform, base=context.cwd
            )
            roots = context.roots() or [context.cwd]
            if not any(
                _inside_target(
                    resolved, root, context.platform, canonical=False
                )
                for root in roots
            ):
                raise ExecutionError(
                    "path_outside_workspace",
                    f"path is outside authorized workspace roots: {resolved}",
                )
            return resolved
        candidate = (
            path if os.path.isabs(path)
            else os.path.join(context.cwd, path)
        )
        resolved = os.path.realpath(os.path.abspath(os.path.expanduser(candidate)))
        roots = context.roots() or [context.cwd]
        if not any(_inside(resolved, root) for root in roots):
            raise ExecutionError(
                "path_outside_workspace",
                f"path is outside authorized workspace roots: {resolved}",
            )
        return resolved

    async def read_file(
            self, user_id: str, meta: dict, path: str, start_line: int = 1,
            end_line: Optional[int] = None, max_chars: int = 100_000,
    ) -> dict[str, Any]:
        context = self.active_context(user_id, meta)
        self._assert_standalone_fs_available()
        resolved = self._resolve(context, path)
        result = await self.appserver.fs_read_file(resolved)
        raw = base64.b64decode((result or {}).get("dataBase64") or "")
        try:
            text = raw.decode("utf-8")
            lines = text.splitlines()
            start = max(1, int(start_line or 1))
            end = min(len(lines), int(end_line or len(lines)))
            content = "\n".join(lines[start - 1:end])
            truncated = len(content) > max_chars
            if truncated:
                content = content[:max_chars]
            return {
                "path": resolved, "encoding": "utf-8", "sizeBytes": len(raw),
                "startLine": start, "endLine": end, "totalLines": len(lines),
                "content": content, "dataBase64": "", "truncated": truncated,
            }
        except UnicodeDecodeError:
            return {
                "path": resolved, "encoding": "base64", "sizeBytes": len(raw),
                "startLine": 0, "endLine": 0, "totalLines": 0, "content": "",
                "dataBase64": base64.b64encode(raw).decode(),
                "truncated": False,
            }

    async def write_file(
            self, user_id: str, meta: dict, path: str, content: str,
            *, envelope: OperationEnvelope,
    ) -> dict[str, Any]:
        self._assert_standalone_fs_available()
        context = await self.validate_operation(user_id, meta, envelope)
        resolved = self._resolve(context, path)
        approved_path = self._resolve(
            context, str(envelope.arguments.get("path") or "")
        )
        approved_content = str(envelope.arguments.get("content") or "")
        if resolved != approved_path or content != approved_content:
            raise ExecutionError(
                "execution_action_changed",
                "file path or content changed after approval",
            )
        encoded = approved_content.encode("utf-8")
        existed = True
        try:
            previous = await self.appserver.fs_read_file(resolved)
            previous_bytes = base64.b64decode(
                (previous or {}).get("dataBase64") or "", validate=True
            )
        except Exception as exc:
            if not _is_missing_file_error(exc):
                raise ExecutionError(
                    "write_file_diff_failed",
                    f"could not read previous file contents: {exc}",
                ) from exc
            existed = False
            previous_bytes = b""
        display = _display_path(context, resolved, path)
        diff, diff_truncated = _write_file_diff(
            display, previous_bytes, encoded, existed
        )
        changed = not existed or previous_bytes != encoded
        await self.appserver.fs_write_file(
            resolved, base64.b64encode(encoded).decode()
        )
        return {
            "conversationId": context.conversation_id,
            "path": resolved, "encoding": "utf-8",
            "bytesWritten": len(encoded), "written": True,
            "changed": changed,
            "fileChanges": [display] if changed else [],
            "diff": diff, "diffTruncated": diff_truncated,
        }

    async def list_dir(
            self, user_id: str, meta: dict, path: str = ""
    ) -> dict[str, Any]:
        context = self.active_context(user_id, meta)
        self._assert_standalone_fs_available()
        return await self.appserver.fs_read_directory(
            self._resolve(context, path or context.cwd)
        )

    async def search_files(
            self, user_id: str, meta: dict, query: str, path: str = ""
    ) -> dict[str, Any]:
        context = self.active_context(user_id, meta)
        self._assert_standalone_fs_available()
        result = await self.appserver.fuzzy_search(
            query, [self._resolve(context, path or context.cwd)]
        )
        files = (result or {}).get("files") or []
        files.sort(key=lambda item: (
            "__pycache__" in str(item.get("path", "")).lower()
            or str(item.get("path", "")).lower().endswith((".pyc", ".pyo")),
            -float(item.get("score") or 0),
        ))
        if isinstance(result, dict):
            result["files"] = files
        return result

    async def exec(
            self, user_id: str, meta: dict, command: list[str], cwd: str,
            timeout_ms: Optional[int], *, envelope: OperationEnvelope,
            require_escalated: bool = False,
    ) -> dict[str, Any]:
        context = await self.validate_operation(user_id, meta, envelope)
        resolved_cwd = self._resolve(context, cwd or context.cwd)
        approved_command = list(envelope.arguments.get("command") or [])
        approved_cwd = self._resolve(
            context, str(envelope.arguments.get("cwd") or context.cwd)
        )
        approved_timeout = envelope.arguments.get("timeoutMs")
        approved_escalated = bool(
            envelope.arguments.get("requireEscalated", False)
        )
        if (
            list(command) != approved_command
            or resolved_cwd != approved_cwd
            or timeout_ms != approved_timeout
            or bool(require_escalated) != approved_escalated
        ):
            raise ExecutionError(
                "execution_action_changed",
                "command arguments or execution settings changed after approval",
            )
        sandbox_policy = self._command_sandbox(
            context, escalated=approved_escalated
        )
        if context.permission_profile_id:
            return await self.appserver.exec_command(
                approved_command, approved_cwd, approved_timeout, None,
                permission_profile_id=context.permission_profile_id,
            )
        return await self.appserver.exec_command(
            approved_command, approved_cwd, approved_timeout, sandbox_policy
        )

    async def apply_patch(
            self, user_id: str, meta: dict, patch: str,
            *, envelope: OperationEnvelope,
    ) -> dict[str, Any]:
        context = await self.validate_operation(user_id, meta, envelope)
        approved_patch = str(envelope.arguments.get("patch") or "")
        if patch != approved_patch:
            raise ExecutionError(
                "execution_action_changed",
                "patch content changed after approval",
            )
        file_changes = self._validate_patch_paths(context, approved_patch)
        resolver = getattr(self.appserver, "codex_command_for_exec", None)
        executable = resolver() if callable(resolver) else ""
        executable = str(executable or self.settings.codex_command or "codex")
        result = await self.appserver.exec_command(
            [executable, "--codex-run-as-apply-patch", approved_patch],
            context.cwd, 120_000, self._command_sandbox(context),
        )
        exit_code = int((result or {}).get("exitCode", -1))
        if exit_code != 0:
            detail = (
                (result or {}).get("stderr")
                or (result or {}).get("stdout")
                or f"Codex apply_patch exited with {exit_code}"
            )
            raise ExecutionError("apply_patch_failed", str(detail)[-4000:])
        # Surface the approved patch so the widget can render a real diff. The
        # frontend parseCodexPatch understands the Codex apply_patch format
        # directly (the same shape the ChatGPT host parses), so no server-side
        # reconstruction is needed.
        diff = approved_patch
        diff_truncated = False
        if len(diff) > MAX_WIDGET_DIFF_CHARS:
            suffix = "\n… Diff truncated for display …\n"
            diff = diff[:MAX_WIDGET_DIFF_CHARS - len(suffix)] + suffix
            diff_truncated = True
        return {
            "conversationId": context.conversation_id,
            "applied": True, "fileChanges": file_changes,
            "diff": diff, "diffTruncated": diff_truncated,
        }

    # ---- downstream MCP tool forwarding ----
    def _mcp_tool_policy(self, server: str, tool: str) -> str:
        """Return allow|ask|deny for one downstream tool (default deny)."""
        raw = (self.store.get("mcp_tool_policy") if self.store else None) or {}
        if not isinstance(raw, dict):
            return MCP_TOOL_POLICY_DEFAULT
        value = str(raw.get(f"{server}/{tool}", MCP_TOOL_POLICY_DEFAULT))
        return value if value in MCP_TOOL_POLICIES else MCP_TOOL_POLICY_DEFAULT

    def set_mcp_tool_policy(self, policies: dict[str, str]) -> dict[str, str]:
        """Persist allow/ask/deny for a set of server/tool keys."""
        if not isinstance(policies, dict):
            raise ExecutionError("invalid_policy", "mcp tool policy must be an object")
        raw = (self.store.get("mcp_tool_policy") if self.store else None) or {}
        current = dict(raw) if isinstance(raw, dict) else {}
        for key, value in policies.items():
            name = str(key)
            if "/" not in name:
                raise ExecutionError(
                    "invalid_policy", f"policy key must be server/tool: {name}")
            decision = str(value)
            if decision not in MCP_TOOL_POLICIES:
                raise ExecutionError(
                    "invalid_policy",
                    f"policy for {name} must be one of allow/ask/deny",
                )
            if decision == MCP_TOOL_POLICY_DEFAULT:
                current.pop(name, None)
            else:
                current[name] = decision
        if self.store:
            self.store.set("mcp_tool_policy", current)
        return current

    def mcp_tool_policies(self) -> dict[str, str]:
        raw = (self.store.get("mcp_tool_policy") if self.store else None) or {}
        return dict(raw) if isinstance(raw, dict) else {}

    async def list_mcp_tools(
            self, user_id: str, meta: dict, *, refresh: bool = False
    ) -> dict[str, Any]:
        """Enumerate downstream MCP tools visible to WebChat, with policy.

        Uses a thread-free status listing when available, else falls back to a
        carrier thread.  Each tool is annotated with its Gateway policy and the
        read-only hint so the panel and the model can reason about exposure.
        """
        context = self.active_context(user_id, meta)
        response: dict[str, Any] = {}
        try:
            response = await self.appserver.mcp_server_status_list()
        except Exception:
            carrier = await self._carrier.thread_id(context.id)
            response = await self.appserver.mcp_server_status_list(carrier)
        servers = (response or {}).get("data") or []
        out_servers: list[dict[str, Any]] = []
        for server in servers:
            name = str(server.get("name") or "")
            raw_tools = server.get("tools") or {}
            tool_items = list(raw_tools.values()) if isinstance(
                raw_tools, dict) else list(raw_tools or [])
            tools: list[dict[str, Any]] = []
            for tool in tool_items:
                tool_name = str(tool.get("name") or "")
                annotations = tool.get("annotations") or {}
                read_only = bool(annotations.get("readOnlyHint"))
                tools.append({
                    "name": tool_name,
                    "description": str(tool.get("description") or ""),
                    "inputSchema": tool.get("inputSchema")
                        or tool.get("input_schema") or {},
                    "readOnly": read_only,
                    "policy": self._mcp_tool_policy(name, tool_name),
                })
            tools.sort(key=lambda item: item["name"])
            out_servers.append({
                "name": name,
                "authStatus": str(server.get("authStatus") or ""),
                "tools": tools,
            })
        out_servers.sort(key=lambda item: item["name"])
        return {"conversationId": context.conversation_id, "servers": out_servers}

    async def mcp_tool_call(
            self, user_id: str, meta: dict, server: str, tool: str,
            arguments: Optional[dict], *, envelope: OperationEnvelope,
            timeout_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        """Execute an approved MCP tool call on the context's carrier thread."""
        context = await self.validate_operation(user_id, meta, envelope)
        if self._mcp_tool_policy(server, tool) == "deny":
            raise ExecutionError(
                "mcp_tool_forbidden",
                f"MCP tool is not exposed to WebChat: {server}/{tool}",
            )
        approved = envelope.arguments
        if (
            str(approved.get("server") or "") != server
            or str(approved.get("tool") or "") != tool
            or (approved.get("arguments") or {}) != (arguments or {})
        ):
            raise ExecutionError(
                "execution_action_changed",
                "MCP tool name or arguments changed after approval",
            )
        carrier = await self._carrier.thread_id(context.id)
        effective_timeout = (
            timeout_ms if isinstance(timeout_ms, int) and timeout_ms > 0
            else MCP_TOOL_TIMEOUT_MS
        )
        result = await self.appserver.mcp_tool_call(
            carrier, server, tool, arguments or {},
            timeout=max(1.0, effective_timeout / 1000.0),
        )
        return {
            "conversationId": context.conversation_id,
            "server": server,
            "tool": tool,
            "content": (result or {}).get("content") or [],
            "structuredContent": (result or {}).get("structuredContent"),
            "isError": bool((result or {}).get("isError")),
        }

    @staticmethod
    def _command_sandbox(
            context: ExecutionContext, escalated: bool = False
    ) -> dict[str, Any]:
        if context.sandbox_mode == "danger-full-access":
            return {"type": "dangerFullAccess"}
        if context.sandbox_mode == "read-only" and not escalated:
            return {"type": "readOnly", "networkAccess": False}
        roots = context.roots() or [context.cwd]
        return {
            "type": "workspaceWrite",
            "writableRoots": roots,
            "networkAccess": False,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        }

    def _validate_patch_paths(
            self, context: ExecutionContext, patch: str
    ) -> list[str]:
        if not patch.startswith("*** Begin Patch") or "*** End Patch" not in patch:
            raise ExecutionError(
                "invalid_patch", "patch must use Codex apply_patch format"
            )
        changes: list[str] = []
        for match in re.finditer(
            r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$"
            r"|^\*\*\* Move to:\s*(.+?)\s*$",
            patch, flags=re.MULTILINE,
        ):
            candidate = next(
                group for group in match.groups() if group is not None
            )
            self._resolve(context, candidate)
            if candidate not in changes:
                changes.append(candidate)
        if not changes:
            raise ExecutionError(
                "invalid_patch", "patch contains no file operation headers"
            )
        return changes

    async def context_status(
            self, user_id: str, meta: dict
    ) -> dict[str, Any]:
        context = self.active_context(user_id, meta)
        return {
            "conversationId": context.conversation_id,
            "contextId": context.id,
            "contextVersion": context.version,
            "status": "ready",
            "pending": False,
            "context": ExecutionRegistry.to_dict(context),
            "capabilities": self.router.capabilities(),
        }

    async def update_plan(
            self, user_id: str, meta: dict, plan: list[dict],
            explanation: str = "",
    ) -> dict[str, Any]:
        context = self.active_context(user_id, meta)
        statuses = [str(item.get("status") or "pending") for item in plan]
        if any(status not in {"pending", "in_progress", "completed"}
               for status in statuses):
            raise ExecutionError("invalid_plan", "unsupported plan status")
        if statuses.count("in_progress") > 1:
            raise ExecutionError(
                "invalid_plan", "at most one plan item may be in_progress"
            )
        self._plans[context.conversation_id] = {
            "plan": plan, "explanation": explanation,
        }
        return {
            "conversationId": context.conversation_id,
            "updated": True, "plan": plan, "explanation": explanation,
        }

    async def view_image(
            self, user_id: str, meta: dict, path: str
    ) -> dict[str, Any]:
        context = self.active_context(user_id, meta)
        self._assert_standalone_fs_available()
        resolved = self._resolve(context, path)
        data = await self.appserver.fs_read_file(resolved)
        raw = base64.b64decode((data or {}).get("dataBase64") or "")
        mime = mimetypes.guess_type(resolved)[0] or "application/octet-stream"
        if not mime.startswith("image/"):
            raise ExecutionError("not_an_image", f"unsupported image type: {mime}")
        return {
            "path": resolved, "mimeType": mime,
            "sizeBytes": len(raw), "dataBase64": base64.b64encode(raw).decode(),
        }

    async def browse_dir(self, path: str = "") -> dict[str, Any]:
        if self._is_external():
            return {
                "path": path,
                "parent": None,
                "entries": [],
                "error": (
                    "directory browsing is unavailable for an external App "
                    "Server; enter an absolute remote workspace path"
                ),
            }
        target = os.path.realpath(
            os.path.abspath(os.path.expanduser(path or os.getcwd()))
        )
        if not os.path.isdir(target):
            return {"path": target, "parent": None, "entries": [],
                    "error": "directory does not exist"}
        entries = []
        try:
            with os.scandir(target) as iterator:
                for entry in sorted(iterator, key=lambda item: (
                    not item.is_dir(follow_symlinks=False), item.name.lower()
                ))[:200]:
                    entries.append({
                        "name": entry.name,
                        "path": entry.path,
                        "isDirectory": entry.is_dir(follow_symlinks=False),
                    })
        except OSError as exc:
            return {"path": target, "parent": None, "entries": [],
                    "error": str(exc)}
        parent = os.path.dirname(target)
        return {
            "path": target,
            "parent": parent if parent != target else None,
            "entries": entries,
        }

def _target_path_module(platform_name: str):
    target = str(platform_name or "").lower()
    if target == "windows":
        return ntpath
    if target in {"linux", "darwin", "unix"}:
        return posixpath
    raise ExecutionError(
        "unsupported_appserver_platform",
        f"unsupported App Server target platform: {platform_name or 'unknown'}",
    )


def _normalize_target_path(
        path: str, platform_name: str, *, base: str = "",
        require_absolute: bool = False,
) -> str:
    value = str(path or "").strip()
    if not value:
        raise ExecutionError("invalid_path", "path is required")
    if value.startswith("~"):
        raise ExecutionError(
            "invalid_path",
            "home-relative paths cannot be resolved on an external App Server",
        )
    path_module = _target_path_module(platform_name)
    if platform_name.lower() == "windows" and value.lower().startswith(
        ("\\\\?\\", "\\\\.\\")
    ):
        raise ExecutionError(
            "invalid_path", "Windows device namespace paths are not supported"
        )
    if require_absolute and not path_module.isabs(value):
        raise ExecutionError(
            "invalid_path", "external App Server paths must be absolute"
        )
    candidate = (
        value if path_module.isabs(value)
        else path_module.join(base, value)
    )
    normalized = path_module.normpath(candidate)
    if not path_module.isabs(normalized):
        raise ExecutionError("invalid_path", "path must resolve to an absolute path")
    if platform_name.lower() == "windows":
        drive, tail = ntpath.splitdrive(normalized)
        if not drive or not tail.startswith(("\\", "/")):
            raise ExecutionError(
                "invalid_path",
                "external Windows paths must include an absolute drive or UNC share",
            )
    return normalized


def _inside_target(
        path: str, root: str, platform_name: str, *, canonical: bool
) -> bool:
    if canonical:
        return _inside(path, root)
    path_module = _target_path_module(platform_name)
    try:
        normalized_path = path_module.normcase(path_module.normpath(path))
        normalized_root = path_module.normcase(path_module.normpath(root))
        return (
            path_module.commonpath([normalized_path, normalized_root])
            == normalized_root
        )
    except ValueError:
        return False


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath(
            [os.path.normcase(path), os.path.normcase(os.path.realpath(root))]
        ) == os.path.normcase(os.path.realpath(root))
    except ValueError:
        return False


def _display_path(
        context: ExecutionContext, resolved: str, requested: str
) -> str:
    path_module = _target_path_module(context.platform)
    if requested and not path_module.isabs(requested):
        display = path_module.normpath(requested)
    else:
        try:
            relative = path_module.relpath(resolved, context.cwd)
            display = relative if not relative.startswith("..") else resolved
        except ValueError:
            display = resolved
    return display.replace("\\", "/")
