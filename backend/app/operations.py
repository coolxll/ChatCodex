"""Ownership routing and conservative policy for standalone App Server RPCs."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any, Optional

from .models import ExecutionContext


READ_ONLY_OPERATIONS = {
    "read_file", "list_dir", "search_files", "view_image",
    "fs/readFile", "fs/readDirectory", "fs/getMetadata", "fuzzyFileSearch",
    "config/read", "configRequirements/read", "permissionProfile/list",
}
GATEWAY_OPERATIONS = {
    "exec_command", "write_file", "apply_patch", "mcp/tool/call",
    "command/exec", "fs/writeFile",
}
# The current official App Server does not guarantee native approval for any
# standalone mutation RPC. Add methods only after an official protocol
# contract exists and OperationRouter.native_operation is used around the call.
NATIVE_APPROVAL_OPERATIONS: set[str] = set()


@dataclass(frozen=True)
class OperationEnvelope:
    operation_id: str
    conversation_id: str
    user_id: str
    method: str
    arguments: dict[str, Any]
    action_digest: str
    execution_context: dict[str, Any]
    context_version: int
    approval_owner: str
    created_at: float

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("arguments", None)
        return {
            "operationId": data["operation_id"],
            "conversationId": data["conversation_id"],
            "method": data["method"],
            "actionDigest": data["action_digest"],
            "contextVersion": data["context_version"],
            "approvalOwner": data["approval_owner"],
            "createdAt": data["created_at"],
        }


@dataclass(frozen=True)
class PolicyDecision:
    action: str  # auto | ask | forbid
    reason: str
    owner: str
    targets: tuple[str, ...] = ()
    execpolicy_mode: str = "not-applicable"
    execpolicy_decision: Optional[str] = None
    matched_rules: int = 0


@dataclass(frozen=True)
class ExecPolicyResult:
    mode: str
    decision: Optional[str] = None
    matched_rules: int = 0
    error: str = ""
    rule_files: tuple[str, ...] = ()
    fingerprint: str = ""


class ExecPolicyProbe:
    """Use official `codex execpolicy check` as a rule-only constraint."""

    def __init__(self, appserver: Any, settings: Any):
        self.appserver = appserver
        self.settings = settings

    async def evaluate(
            self, command: list[str], cwd: str, timeout: float = 5.0
    ) -> ExecPolicyResult:
        if str(getattr(self.settings, "codex_app_mode", "internal")) != "internal":
            return ExecPolicyResult(
                mode="unavailable", fingerprint="external:unavailable"
            )
        try:
            config = await self.appserver.call(
                "config/read", {"cwd": cwd, "includeLayers": True}, timeout=8.0
            )
            rule_files = self._rule_files(config or {})
        except Exception as exc:
            return ExecPolicyResult(
                mode="rules-only", error=f"config/read failed: {exc}"[:500]
            )
        try:
            fingerprint = self._fingerprint(config or {}, rule_files)
        except OSError as exc:
            return ExecPolicyResult(
                mode="rules-only",
                error=f"could not fingerprint exec-policy rules: {exc}"[:500],
                rule_files=tuple(rule_files),
            )
        if not rule_files:
            return ExecPolicyResult(
                mode="rules-only", rule_files=(), fingerprint=fingerprint
            )

        resolver = getattr(self.appserver, "codex_command_for_exec", None)
        executable = resolver() if callable(resolver) else ""
        executable = str(executable or getattr(self.settings, "codex_command", "") or "codex")
        argv: list[str] = [executable, "execpolicy", "check"]
        for path in rule_files:
            argv.extend(["--rules", path])
        argv.extend(["--resolve-host-executables", "--", *command])
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            return ExecPolicyResult(
                mode="rules-only", error="execpolicy check timed out",
                rule_files=tuple(rule_files), fingerprint=fingerprint,
            )
        except Exception as exc:
            return ExecPolicyResult(
                mode="rules-only", error=str(exc)[:500],
                rule_files=tuple(rule_files), fingerprint=fingerprint,
            )
        if proc.returncode != 0:
            return ExecPolicyResult(
                mode="rules-only",
                error=(stderr.decode("utf-8", "replace") or
                       stdout.decode("utf-8", "replace"))[-500:],
                rule_files=tuple(rule_files), fingerprint=fingerprint,
            )
        try:
            data = json.loads(stdout.decode("utf-8"))
            matches = data.get("matchedRules") or []
            decision = data.get("decision")
            if decision is not None:
                decision = str(decision).lower()
            return ExecPolicyResult(
                mode="rules-only",
                decision=decision,
                matched_rules=len(matches) if isinstance(matches, list) else 0,
                rule_files=tuple(rule_files),
                fingerprint=fingerprint,
            )
        except Exception as exc:
            return ExecPolicyResult(
                mode="rules-only", error=f"invalid execpolicy output: {exc}"[:500],
                rule_files=tuple(rule_files), fingerprint=fingerprint,
            )

    @staticmethod
    def _fingerprint(
            config_response: dict[str, Any], rule_files: list[str]
    ) -> str:
        layers = []
        for layer in config_response.get("layers") or []:
            if not isinstance(layer, dict):
                continue
            layers.append({
                "name": layer.get("name"),
                "version": layer.get("version"),
                "disabledReason": layer.get("disabledReason"),
            })
        digest = hashlib.sha256(json.dumps(
            layers, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8"))
        for filename in rule_files:
            digest.update(b"\0")
            digest.update(filename.encode("utf-8", "surrogatepass"))
            digest.update(b"\0")
            digest.update(Path(filename).read_bytes())
        return digest.hexdigest()

    @staticmethod
    def _rule_files(config_response: dict[str, Any]) -> list[str]:
        files: list[str] = []
        for layer in config_response.get("layers") or []:
            if not isinstance(layer, dict) or layer.get("disabledReason"):
                continue
            source = layer.get("name") or {}
            if not isinstance(source, dict):
                continue
            kind = str(source.get("type") or "")
            folder = ""
            if kind in {"system", "user", "legacyManagedConfigTomlFromFile"}:
                file_path = str(source.get("file") or "")
                if file_path:
                    folder = os.path.dirname(file_path)
            elif kind == "project":
                folder = str(
                    source.get("dotCodexFolder")
                    or source.get("dot_codex_folder")
                    or ""
                )
            if not folder:
                continue
            rules_dir = Path(folder) / "rules"
            try:
                candidates = sorted(rules_dir.glob("*.rules"))
            except OSError:
                continue
            for candidate in candidates:
                try:
                    resolved = str(candidate.resolve(strict=True))
                except OSError:
                    continue
                if resolved not in files:
                    files.append(resolved)
        return files


class OperationRouter:
    """Assign exactly one approval owner before a standalone RPC executes."""

    def __init__(self, appserver: Any, settings: Any):
        self.execpolicy = ExecPolicyProbe(appserver, settings)
        self.settings = settings

    @staticmethod
    def owner_for(method: str) -> str:
        if method in READ_ONLY_OPERATIONS:
            return "none"
        if method in NATIVE_APPROVAL_OPERATIONS:
            return "appserver"
        if method in GATEWAY_OPERATIONS:
            return "gateway"
        return "forbidden"

    def envelope(
            self, context: ExecutionContext, user_id: str, method: str,
            arguments: dict[str, Any], *, policy_fingerprint: str = "",
            requirements_fingerprint: str = "",
    ) -> OperationEnvelope:
        owner = self.owner_for(method)
        digest_payload = {
            "method": method,
            "arguments": arguments,
            "conversationId": context.conversation_id,
            "cwd": context.cwd,
            "workspaceRoots": context.roots(),
            "sandboxMode": context.sandbox_mode,
            "permissionProfileId": context.permission_profile_id,
            "approvalPolicy": context.approval_policy,
            "workMode": context.work_mode,
            "platform": context.platform,
            "contextVersion": context.version,
            "appServerInstanceId": context.appserver_instance_id,
            "execPolicyFingerprint": policy_fingerprint,
            "requirementsFingerprint": requirements_fingerprint,
        }
        encoded = json.dumps(
            digest_payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return OperationEnvelope(
            operation_id=f"op-{secrets.token_hex(12)}",
            conversation_id=context.conversation_id,
            user_id=user_id,
            method=method,
            arguments=arguments,
            action_digest=hashlib.sha256(encoded).hexdigest(),
            execution_context={
                "cwd": context.cwd,
                "workspaceRoots": context.roots(),
                "sandboxMode": context.sandbox_mode,
                "permissionProfileId": context.permission_profile_id,
                "approvalPolicy": context.approval_policy,
                "workMode": context.work_mode,
                "platform": context.platform,
                "appServerInstanceId": context.appserver_instance_id,
                "execPolicyFingerprint": policy_fingerprint,
                "requirementsFingerprint": requirements_fingerprint,
            },
            context_version=context.version,
            approval_owner=owner,
            created_at=time.time(),
        )

    async def decide(
            self, context: ExecutionContext, envelope: OperationEnvelope,
            targets: tuple[str, ...] = (),
            execpolicy_result: Optional[ExecPolicyResult] = None,
    ) -> PolicyDecision:
        owner = envelope.approval_owner
        if owner == "forbidden":
            return PolicyDecision(
                "forbid", f"unregistered side-effecting operation: {envelope.method}",
                owner, targets,
            )
        if owner == "none":
            return PolicyDecision("auto", "read-only operation", owner, targets)
        if owner == "appserver":
            return PolicyDecision(
                "auto", "official protocol guarantees native approval", owner, targets
            )
        if context.work_mode == "plan":
            return PolicyDecision(
                "forbid", "exec and file changes are disabled in Plan mode",
                owner, targets,
            )

        policy = self._approval_policy(context.approval_policy)
        if policy == "never":
            return PolicyDecision(
                "forbid",
                "standalone mutations are disabled when approvals are unavailable",
                owner, targets,
            )
        if policy in {"granular-deny", "invalid"}:
            return PolicyDecision(
                "forbid",
                (
                    "granular policy disables this approval category"
                    if policy == "granular-deny"
                    else "approval policy could not be evaluated"
                ),
                owner, targets,
            )

        if envelope.method == "exec_command":
            command = envelope.arguments.get("command") or []
            probe = execpolicy_result or await self.execpolicy.evaluate(
                command,
                str(envelope.arguments.get("cwd") or context.cwd),
            )
            if probe.error:
                return PolicyDecision(
                    "forbid",
                    f"official exec-policy rule check failed: {probe.error}",
                    owner, targets, probe.mode, probe.decision,
                    probe.matched_rules,
                )
            if probe.decision == "forbidden":
                return PolicyDecision(
                    "forbid", "an official exec-policy rule forbids this command",
                    owner, targets, probe.mode, probe.decision,
                    probe.matched_rules,
                )
            reason = (
                "official App Server command/exec has no native approval; "
                "Gateway requires one-time approval"
            )
            if probe.decision:
                reason += f" (rules-only result: {probe.decision})"
            return PolicyDecision(
                "ask", reason, owner, targets, probe.mode, probe.decision,
                probe.matched_rules,
            )

        if envelope.method == "mcp/tool/call":
            server = str(envelope.arguments.get("server") or "")
            tool = str(envelope.arguments.get("tool") or "")
            read_only = bool(
                envelope.arguments.get("annotations", {}).get("readOnlyHint")
            ) if isinstance(envelope.arguments.get("annotations"), dict) else False
            if read_only:
                return PolicyDecision(
                    "auto",
                    f"read-only MCP tool {server}/{tool} runs directly",
                    owner, targets,
                )
            return PolicyDecision(
                "ask",
                f"MCP tool {server}/{tool} may have side effects and the idle "
                "carrier thread applies no native approval; Gateway requires "
                "one-time approval",
                owner, targets,
            )

        return PolicyDecision(
            "ask",
            "official App Server standalone file RPC has no native approval",
            owner, targets,
        )

    @staticmethod
    def _approval_policy(value: str) -> str:
        raw = str(value or "on-request")
        if not raw.startswith("{"):
            return raw
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return "invalid"
        if not isinstance(decoded, dict):
            return "invalid"
        granular = decoded.get("granular", decoded)
        if not isinstance(granular, dict):
            return "invalid"
        allowed = granular.get(
            "sandbox_approval", granular.get("sandboxApproval")
        )
        if not isinstance(allowed, bool):
            return "invalid"
        if allowed is False:
            return "granular-deny"
        return "granular"

    def capabilities(self) -> dict[str, Any]:
        internal = str(getattr(self.settings, "codex_app_mode", "internal")) == "internal"
        return {
            "appServerMode": "internal" if internal else "external",
            "nativeStandaloneApprovals": sorted(NATIVE_APPROVAL_OPERATIONS),
            "execPolicyMode": "rules-only" if internal else "unavailable",
            "fallbackApproval": True,
            "eventTransport": "sse",
            "codexAgentSessions": False,
            "mcpForwarding": True,
            "standaloneFilesystem": (
                "available" if internal else "unavailable"
            ),
            "remoteFilesystemBoundary": (
                "verified-local" if internal else "unavailable"
            ),
        }
