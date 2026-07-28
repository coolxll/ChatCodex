from __future__ import annotations

import asyncio
import os
import platform
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.approval import ApprovalBridge
from app.appserver.rpc_methods import CodexRpcMethods
from app.config import Settings
from app.db import Database
from app.events import EventBroker
from app.execution import ExecutionError, ExecutionOrchestrator
from app.mcp_server import build_mcp
from app.models import ExecutionRegistry
from app.operations import ExecPolicyProbe, OperationRouter


class ThreadFreeExecutionContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = Database(Settings(
            database_url=f"sqlite:///{root / 'contracts.db'}",
        ))
        self.registry = ExecutionRegistry(self.db)
        self.settings = Settings(codex_app_mode="internal", mcp_auth_mode="noauth")
        self.appserver = SimpleNamespace(
            status=Mock(return_value={
                "instanceId": "appserver-1",
                "platformOs": platform.system().lower(),
            }),
            exec_command=AsyncMock(return_value={
                "exitCode": 0, "stdout": "ok", "stderr": "",
            }),
            fs_read_file=AsyncMock(),
            fs_write_file=AsyncMock(),
            fs_read_directory=AsyncMock(return_value={"entries": []}),
            fuzzy_search=AsyncMock(return_value={"files": []}),
            mcp_status_list=AsyncMock(return_value={"data": []}),
            call=AsyncMock(return_value={"requirements": None}),
        )
        self.orch = ExecutionOrchestrator(
            self.settings, self.appserver, self.registry,
        )
        self.meta = {"conversationId": "conversation-a"}

    def use_external_appserver(self) -> None:
        self.settings = Settings(
            codex_app_mode="external", mcp_auth_mode="noauth"
        )
        self.orch = ExecutionOrchestrator(
            self.settings, self.appserver, self.registry,
        )

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    async def configure(
            self, *, approval: str = "on-request", work_mode: str = "agent",
    ):
        return await self.orch.configure_context("user-a", self.meta, {
            "cwd": self.tmp.name,
            "workspaceRoots": [self.tmp.name],
            "sandbox": "workspace-write",
            "approvalPolicy": approval,
            "workMode": work_mode,
        })

    async def test_configure_creates_gateway_context_without_agent_rpc(self):
        result = await self.configure()
        self.assertEqual(result["conversationId"], "conversation-a")
        self.assertFalse(result["codexAgentSession"])
        self.assertEqual(result["contextVersion"], 1)
        self.assertEqual(self.appserver.call.await_count, 0)
        # Only an idle ephemeral carrier thread + MCP forwarding RPCs exist;
        # there is still no way to start a model turn through this adapter.
        self.assertFalse(hasattr(CodexRpcMethods, "turn_start"))
        self.assertFalse(hasattr(CodexRpcMethods, "thread_resume"))
        import inspect as _inspect
        self.assertTrue(
            _inspect.signature(
                CodexRpcMethods.thread_start
            ).parameters["ephemeral"].default
        )

    async def test_external_exec_uses_conservative_gateway_owner(self):
        self.use_external_appserver()
        await self.configure()
        _, envelope, decision = await self.orch.prepare_operation(
            "user-a", self.meta, "exec_command",
            {"command": ["node", "--check", "app.js"], "cwd": self.tmp.name},
        )
        self.assertEqual(envelope.approval_owner, "gateway")
        self.assertEqual(decision.action, "ask")
        self.assertEqual(decision.execpolicy_mode, "unavailable")
        self.appserver.call.assert_awaited_once_with(
            "configRequirements/read", None, timeout=8.0
        )

    async def test_external_context_uses_reported_remote_path_platform(self):
        self.use_external_appserver()
        self.appserver.status.return_value = {
            "instanceId": "appserver-1", "platformOs": "linux",
        }
        result = await self.orch.configure_context("user-a", self.meta, {
            "cwd": "/srv/work/project",
            "workspaceRoots": ["/srv/work"],
            "sandbox": "workspace-write",
            "approvalPolicy": "on-request",
            "workMode": "agent",
        })
        self.assertEqual(result["cwd"], "/srv/work/project")
        self.assertEqual(result["platform"], "linux")
        context, envelope, decision = await self.orch.prepare_operation(
            "user-a", self.meta, "exec_command",
            {"command": ["node", "--check", "app.js"], "cwd": "."},
        )
        self.assertEqual(envelope.arguments["cwd"], "/srv/work/project")
        self.assertEqual(envelope.execution_context["platform"], "linux")
        self.assertEqual(decision.action, "ask")
        self.assertEqual(
            self.orch._command_sandbox(context)["writableRoots"],
            ["/srv/work"],
        )

    async def test_external_standalone_filesystem_fails_closed(self):
        self.use_external_appserver()
        await self.configure()
        with self.assertRaisesRegex(
            ExecutionError, "canonical workspace boundaries"
        ):
            await self.orch.read_file(
                "user-a", self.meta, "app.js"
            )
        with self.assertRaisesRegex(
            ExecutionError, "canonical workspace boundaries"
        ):
            await self.orch.prepare_operation(
                "user-a", self.meta, "write_file",
                {"path": "app.js", "content": "x"},
            )
        self.appserver.fs_read_file.assert_not_awaited()
        self.appserver.fs_write_file.assert_not_awaited()

    async def test_external_context_requires_reported_platform(self):
        self.use_external_appserver()
        self.appserver.status.return_value = {"instanceId": "appserver-1"}
        with self.assertRaisesRegex(
            ExecutionError, "did not report its target platform"
        ):
            await self.configure()

    async def test_approval_wait_does_not_start_or_consume_command_timeout(self):
        await self.configure()
        context, envelope, _ = await self.orch.prepare_operation(
            "user-a", self.meta, "exec_command",
            {
                "command": ["node", "--check", "app.js"],
                "cwd": self.tmp.name,
                "timeoutMs": 10,
                "requireEscalated": False,
                "justification": "",
            },
        )
        events = EventBroker()
        bridge = ApprovalBridge(self.appserver, self.db, events=events)

        async def execute():
            return await self.orch.exec(
                "user-a", self.meta,
                ["node", "--check", "app.js"], self.tmp.name, 10,
                envelope=envelope,
            )

        task = asyncio.create_task(bridge.run_gateway_operation(
            envelope=envelope,
            kind="commandExecution",
            message="approve",
            params={"command": ["node", "--check", "app.js"]},
            user_id="user-a",
            execute=execute,
            validate=lambda: self.orch.validate_operation(
                "user-a", self.meta, envelope
            ),
            timeout=1.0,
        ))
        await asyncio.sleep(0.05)
        self.appserver.exec_command.assert_not_awaited()
        pending = bridge.list_pending(context.conversation_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], "pending")
        self.assertTrue(await bridge.resolve(
            pending[0]["requestId"], {"action": "approve_once"},
            conversation_id=context.conversation_id,
            expected_version=pending[0]["version"],
        ))
        result = await task
        self.assertEqual(result["exitCode"], 0)
        self.appserver.exec_command.assert_awaited_once()
        self.assertEqual(self.appserver.exec_command.await_args.args[2], 10)
        event_names = [
            item.event for item in events._history[context.conversation_id]
        ]
        self.assertEqual(event_names, [
            "approval.created", "approval.updated", "approval.resolved",
            "operation.executing", "operation.completed",
        ])

    async def test_context_change_after_approval_prevents_execution(self):
        await self.configure()
        context, envelope, _ = await self.orch.prepare_operation(
            "user-a", self.meta, "exec_command",
            {"command": ["node", "--check", "app.js"], "cwd": self.tmp.name},
        )
        bridge = ApprovalBridge(self.appserver, self.db)
        task = asyncio.create_task(bridge.run_gateway_operation(
            envelope=envelope,
            kind="commandExecution",
            message="approve",
            params={"command": ["node", "--check", "app.js"]},
            user_id="user-a",
            execute=lambda: self.orch.exec(
                "user-a", self.meta, ["node", "--check", "app.js"],
                self.tmp.name, 10, envelope=envelope,
            ),
            validate=lambda: self.orch.validate_operation(
                "user-a", self.meta, envelope
            ),
            timeout=1.0,
        ))
        await asyncio.sleep(0)
        pending = bridge.list_pending(context.conversation_id)[0]
        await self.configure()
        self.assertTrue(await bridge.resolve(
            pending["requestId"], {"action": "approve_once"},
            conversation_id=context.conversation_id,
            expected_version=pending["version"],
        ))
        with self.assertRaisesRegex(ExecutionError, "changed while approval"):
            await task
        self.appserver.exec_command.assert_not_awaited()

    async def test_approved_exec_rejects_replaced_arguments(self):
        await self.configure()
        _, envelope, _ = await self.orch.prepare_operation(
            "user-a", self.meta, "exec_command",
            {
                "command": ["node", "--check", "app.js"],
                "cwd": self.tmp.name,
                "timeoutMs": 10_000,
                "requireEscalated": False,
                "justification": "",
            },
        )
        with self.assertRaisesRegex(
            ExecutionError, "changed after approval"
        ):
            await self.orch.exec(
                "user-a", self.meta,
                ["node", "--check", "different.js"],
                self.tmp.name, 10_000, envelope=envelope,
            )
        self.appserver.exec_command.assert_not_awaited()

    async def test_approved_write_rejects_replaced_content(self):
        await self.configure()
        _, envelope, _ = await self.orch.prepare_operation(
            "user-a", self.meta, "write_file",
            {"path": "result.txt", "content": "approved"},
        )
        with self.assertRaisesRegex(
            ExecutionError, "changed after approval"
        ):
            await self.orch.write_file(
                "user-a", self.meta, "result.txt", "replacement",
                envelope=envelope,
            )
        self.appserver.fs_write_file.assert_not_awaited()

    async def test_approved_patch_rejects_replaced_content(self):
        await self.configure()
        approved = (
            "*** Begin Patch\n"
            "*** Add File: approved.txt\n"
            "+approved\n"
            "*** End Patch"
        )
        replacement = approved.replace("approved.txt", "replacement.txt")
        _, envelope, _ = await self.orch.prepare_operation(
            "user-a", self.meta, "apply_patch", {"patch": approved},
        )
        with self.assertRaisesRegex(
            ExecutionError, "changed after approval"
        ):
            await self.orch.apply_patch(
                "user-a", self.meta, replacement, envelope=envelope,
            )
        self.appserver.exec_command.assert_not_awaited()

    async def test_never_and_plan_mode_fail_before_pending(self):
        await self.configure(approval="never")
        _, _, never = await self.orch.prepare_operation(
            "user-a", self.meta, "exec_command",
            {"command": ["node", "--check", "app.js"], "cwd": self.tmp.name},
        )
        self.assertEqual(never.action, "forbid")
        await self.configure(work_mode="plan")
        _, _, plan = await self.orch.prepare_operation(
            "user-a", self.meta, "write_file",
            {"path": "result.txt", "content": "blocked"},
        )
        self.assertEqual(plan.action, "forbid")

    async def test_granular_policy_uses_official_sandbox_approval_flag(self):
        allowed = {
            "granular": {
                "sandbox_approval": True,
                "rules": False,
                "mcp_elicitations": False,
            },
        }
        await self.configure(approval=allowed)
        _, _, ask = await self.orch.prepare_operation(
            "user-a", self.meta, "write_file",
            {"path": "result.txt", "content": "allowed"},
        )
        self.assertEqual(ask.action, "ask")

        denied = {
            "granular": {
                "sandbox_approval": False,
                "rules": True,
                "mcp_elicitations": True,
            },
        }
        await self.configure(approval=denied)
        _, _, blocked = await self.orch.prepare_operation(
            "user-a", self.meta, "write_file",
            {"path": "result.txt", "content": "blocked"},
        )
        self.assertEqual(blocked.action, "forbid")

    async def test_appserver_instance_change_requires_reconfiguration(self):
        await self.configure()
        self.appserver.status.return_value = {"instanceId": "appserver-2"}
        with self.assertRaisesRegex(ExecutionError, "instance changed"):
            self.orch.active_context("user-a", self.meta)

    async def test_managed_requirements_change_invalidates_approval(self):
        await self.configure()
        context, envelope, _ = await self.orch.prepare_operation(
            "user-a", self.meta, "write_file",
            {"path": "result.txt", "content": "content"},
        )
        bridge = ApprovalBridge(self.appserver, self.db)
        task = asyncio.create_task(bridge.run_gateway_operation(
            envelope=envelope,
            kind="fileChange",
            message="approve",
            params={},
            user_id="user-a",
            execute=lambda: self.orch.write_file(
                "user-a", self.meta, "result.txt", "content",
                envelope=envelope,
            ),
            validate=lambda: self.orch.validate_operation(
                "user-a", self.meta, envelope
            ),
            timeout=1.0,
        ))
        await asyncio.sleep(0)
        pending = bridge.list_pending(context.conversation_id)[0]
        self.appserver.call.return_value = {
            "requirements": {"allowedSandboxModes": ["read-only"]},
        }
        self.assertTrue(await bridge.resolve(
            pending["requestId"], {"action": "approve_once"},
            conversation_id=context.conversation_id,
            expected_version=pending["version"],
        ))
        with self.assertRaisesRegex(ExecutionError, "sandbox mode"):
            await task
        self.appserver.fs_write_file.assert_not_awaited()

    async def test_workspace_escape_is_rejected_before_approval(self):
        await self.configure()
        outside = str(Path(self.tmp.name).parent / "outside.txt")
        with self.assertRaisesRegex(ExecutionError, "outside authorized"):
            await self.orch.prepare_operation(
                "user-a", self.meta, "write_file",
                {"path": outside, "content": "blocked"},
            )

    async def test_uncorrelated_native_reverse_approval_is_rejected(self):
        bridge = ApprovalBridge(self.appserver, self.db)
        response = await bridge.handle({
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "obsolete-agent-thread"},
        })
        self.assertEqual(response, {"decision": "cancel"})
        self.assertEqual(bridge.list_pending(), [])

    async def test_correlated_native_approval_uses_the_same_queue(self):
        bridge = ApprovalBridge(self.appserver, self.db)
        async with bridge.native_operation(
            conversation_id="conversation-a",
            operation_id="operation-native",
            user_id="user-a",
        ):
            task = asyncio.create_task(bridge.handle({
                "method": "item/fileChange/requestApproval",
                "params": {"approvalId": "upstream-1"},
            }))
            await asyncio.sleep(0)
            pending = bridge.list_pending("conversation-a")
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["source"], "appserver")
            self.assertEqual(pending[0]["operationId"], "operation-native")
            self.assertTrue(await bridge.resolve(
                pending[0]["requestId"], {"action": "accept"},
                conversation_id="conversation-a",
                expected_version=pending[0]["version"],
            ))
            self.assertEqual(await task, {"decision": "accept"})

    async def test_gateway_decisions_are_one_time_and_versioned(self):
        await self.configure()
        context, envelope, _ = await self.orch.prepare_operation(
            "user-a", self.meta, "write_file",
            {"path": "result.txt", "content": "content"},
        )
        bridge = ApprovalBridge(self.appserver, self.db)
        executed = 0

        async def execute():
            nonlocal executed
            executed += 1
            return {"ok": True}

        task = asyncio.create_task(bridge.run_gateway_operation(
            envelope=envelope, kind="fileChange", message="approve",
            params={}, user_id="user-a", execute=execute, timeout=1.0,
        ))
        await asyncio.sleep(0)
        pending = bridge.list_pending(context.conversation_id)[0]
        self.assertFalse(await bridge.resolve(
            pending["requestId"], {"action": "accept"},
            conversation_id=context.conversation_id,
            expected_version=pending["version"],
        ))
        self.assertFalse(await bridge.resolve(
            pending["requestId"], {"action": "approve_once"},
            conversation_id=context.conversation_id,
            expected_version=pending["version"] + 1,
        ))
        self.assertTrue(await bridge.resolve(
            pending["requestId"], {"action": "approve_once"},
            conversation_id=context.conversation_id,
            expected_version=pending["version"],
        ))
        self.assertEqual(await task, {"ok": True})
        self.assertEqual(executed, 1)
        self.assertFalse(await bridge.resolve(
            pending["requestId"], {"action": "approve_once"},
            conversation_id=context.conversation_id,
        ))

    def test_mcp_registers_thread_free_and_mcp_forwarding_tools(self):
        bridge = ApprovalBridge(self.appserver, self.db)
        mcp = build_mcp(self.settings, self.orch, bridge)
        names = set(mcp._tool_manager._tools)
        self.assertEqual(names, {
            "open_workspace_setup", "save_execution_context",
            "execution_status", "resolve_approval",
            "read_file", "write_file", "list_dir", "search_files",
            "exec_command", "apply_patch", "update_plan", "view_image",
            "request_user_input", "browse_dir",
            "mcp_list_tools", "mcp_call_tool",
        })
        self.assertFalse(any(name.startswith("mcp__") for name in names))


class McpForwardingContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = Database(Settings(
            database_url=f"sqlite:///{root / 'mcp.db'}",
        ))
        self.registry = ExecutionRegistry(self.db)
        self.settings = Settings(codex_app_mode="internal", mcp_auth_mode="noauth")
        self.appserver = SimpleNamespace(
            status=Mock(return_value={
                "instanceId": "appserver-1",
                "platformOs": platform.system().lower(),
            }),
            thread_start=AsyncMock(return_value={"thread": {"id": "thread-1"}}),
            mcp_server_status_list=AsyncMock(return_value={"data": [{
                "name": "docs",
                "authStatus": "unsupported",
                "tools": {
                    "search": {
                        "name": "search",
                        "description": "read-only search",
                        "inputSchema": {"type": "object"},
                        "annotations": {"readOnlyHint": True},
                    },
                    "delete": {
                        "name": "delete",
                        "description": "destructive",
                        "inputSchema": {"type": "object"},
                        "annotations": {"readOnlyHint": False},
                    },
                },
            }]}),
            mcp_tool_call=AsyncMock(return_value={
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {"hits": 2},
                "isError": False,
            }),
            call=AsyncMock(return_value={"requirements": None}),
        )
        from app.settings_store import SettingsStore
        self.store = SettingsStore(self.db)
        self.orch = ExecutionOrchestrator(
            self.settings, self.appserver, self.registry, self.store,
        )
        self.meta = {"conversationId": "conversation-a"}

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    async def configure(self, **over):
        cfg = {
            "cwd": self.tmp.name, "workspaceRoots": [self.tmp.name],
            "sandbox": "workspace-write", "approvalPolicy": "on-request",
            "workMode": "agent",
        }
        cfg.update(over)
        return await self.orch.configure_context("user-a", self.meta, cfg)

    async def test_list_mcp_tools_marks_policy_and_readonly(self):
        await self.configure()
        data = await self.orch.list_mcp_tools("user-a", self.meta)
        server = data["servers"][0]
        self.assertEqual(server["name"], "docs")
        by_name = {tool["name"]: tool for tool in server["tools"]}
        self.assertTrue(by_name["search"]["readOnly"])
        self.assertFalse(by_name["delete"]["readOnly"])
        # Unlisted tools default to deny.
        self.assertEqual(by_name["search"]["policy"], "deny")
        self.assertEqual(by_name["delete"]["policy"], "deny")

    async def test_tool_policy_roundtrip_and_validation(self):
        self.orch.set_mcp_tool_policy({"docs/search": "allow", "docs/delete": "ask"})
        self.assertEqual(self.orch.mcp_tool_policies(),
                         {"docs/search": "allow", "docs/delete": "ask"})
        # Reset to default removes the override.
        self.orch.set_mcp_tool_policy({"docs/search": "deny"})
        self.assertEqual(self.orch.mcp_tool_policies(), {"docs/delete": "ask"})
        with self.assertRaisesRegex(ExecutionError, "server/tool"):
            self.orch.set_mcp_tool_policy({"noslash": "allow"})
        with self.assertRaisesRegex(ExecutionError, "allow/ask/deny"):
            self.orch.set_mcp_tool_policy({"docs/search": "bogus"})

    async def test_deny_tool_is_forbidden_before_pending(self):
        await self.configure()
        with self.assertRaisesRegex(ExecutionError, "not exposed"):
            await self.orch.prepare_operation(
                "user-a", self.meta, "mcp/tool/call",
                {"server": "docs", "tool": "search", "arguments": {}},
            )
        self.appserver.mcp_tool_call.assert_not_awaited()

    async def test_readonly_allowed_tool_runs_without_approval(self):
        await self.configure()
        self.orch.set_mcp_tool_policy({"docs/search": "allow"})
        context, envelope, decision = await self.orch.prepare_operation(
            "user-a", self.meta, "mcp/tool/call",
            {"server": "docs", "tool": "search", "arguments": {"q": "x"},
             "annotations": {"readOnlyHint": True}},
        )
        self.assertEqual(decision.action, "auto")
        self.assertEqual(envelope.approval_owner, "gateway")
        result = await self.orch.mcp_tool_call(
            "user-a", self.meta, "docs", "search", {"q": "x"},
            envelope=envelope,
        )
        self.assertEqual(result["structuredContent"], {"hits": 2})
        args = self.appserver.mcp_tool_call.await_args.args
        self.assertEqual(args[1:4], ("docs", "search", {"q": "x"}))

    async def test_side_effect_tool_requires_gateway_approval(self):
        await self.configure()
        self.orch.set_mcp_tool_policy({"docs/delete": "ask"})
        _, _, decision = await self.orch.prepare_operation(
            "user-a", self.meta, "mcp/tool/call",
            {"server": "docs", "tool": "delete", "arguments": {"id": 1},
             "annotations": {"readOnlyHint": False}},
        )
        self.assertEqual(decision.action, "ask")

    async def test_approved_call_rejects_changed_arguments(self):
        await self.configure()
        self.orch.set_mcp_tool_policy({"docs/search": "allow"})
        _, envelope, _ = await self.orch.prepare_operation(
            "user-a", self.meta, "mcp/tool/call",
            {"server": "docs", "tool": "search", "arguments": {"q": "x"},
             "annotations": {"readOnlyHint": True}},
        )
        with self.assertRaisesRegex(ExecutionError, "changed after approval"):
            await self.orch.mcp_tool_call(
                "user-a", self.meta, "docs", "search", {"q": "DIFFERENT"},
                envelope=envelope,
            )
        self.appserver.mcp_tool_call.assert_not_awaited()

    async def test_carrier_thread_reused_and_rebuilt_after_drop(self):
        await self.configure()
        first = await self.orch._carrier.thread_id("ctx")
        self.assertEqual(first, "thread-1")
        # Reused without respawn.
        again = await self.orch._carrier.thread_id("ctx")
        self.assertEqual(again, "thread-1")
        self.assertEqual(self.appserver.thread_start.await_count, 1)
        # Dropping forces a fresh thread on next use.
        self.appserver.thread_start.return_value = {"thread": {"id": "thread-2"}}
        await self.orch._carrier.drop("ctx")
        rebuilt = await self.orch._carrier.thread_id("ctx")
        self.assertEqual(rebuilt, "thread-2")
        self.assertEqual(self.appserver.thread_start.await_count, 2)


class ExecPolicyContracts(unittest.IsolatedAsyncioTestCase):
    async def _decision(self, value: str):
        with tempfile.TemporaryDirectory() as directory:
            rules = Path(directory) / "rules"
            rules.mkdir()
            (rules / "default.rules").write_text("prefix_rule()", encoding="utf-8")
            appserver = SimpleNamespace(
                call=AsyncMock(return_value={"layers": [{
                    "name": {"type": "user", "file": str(Path(directory) / "config.toml")},
                }]}),
                codex_command_for_exec=lambda: "codex",
            )
            settings = Settings(codex_app_mode="internal")
            router = OperationRouter(appserver, settings)
            context = SimpleNamespace(
                conversation_id="conversation", cwd=directory,
                roots=lambda: [directory], sandbox_mode="workspace-write",
                permission_profile_id=None, approval_policy="on-request",
                work_mode="agent", version=1, appserver_instance_id="one",
                platform=platform.system().lower(),
            )
            envelope = router.envelope(
                context, "user", "exec_command",
                {"command": ["node", "--check", "app.js"], "cwd": directory},
            )
            proc = SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(
                    f'{{"decision":"{value}","matchedRules":[{{}}]}}'.encode(),
                    b"",
                )),
                kill=Mock(),
            )
            with patch(
                "app.operations.asyncio.create_subprocess_exec",
                AsyncMock(return_value=proc),
            ):
                return await router.decide(context, envelope)

    async def test_rules_allow_still_requires_gateway_approval(self):
        decision = await self._decision("allow")
        self.assertEqual(decision.action, "ask")
        self.assertEqual(decision.execpolicy_decision, "allow")
        self.assertEqual(decision.matched_rules, 1)

    async def test_rules_forbidden_blocks_execution(self):
        decision = await self._decision("forbidden")
        self.assertEqual(decision.action, "forbid")

    async def test_external_probe_never_reads_local_rules(self):
        appserver = SimpleNamespace(call=AsyncMock())
        result = await ExecPolicyProbe(
            appserver, Settings(codex_app_mode="external")
        ).evaluate(["node", "--check", "app.js"], os.getcwd())
        self.assertEqual(result.mode, "unavailable")
        appserver.call.assert_not_awaited()

    async def test_rule_content_change_changes_policy_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            rules = Path(directory) / "rules"
            rules.mkdir()
            rule_file = rules / "default.rules"
            rule_file.write_text(
                'prefix_rule(pattern=["node"], decision="prompt")',
                encoding="utf-8",
            )
            appserver = SimpleNamespace(
                call=AsyncMock(return_value={"layers": [{
                    "name": {
                        "type": "user",
                        "file": str(Path(directory) / "config.toml"),
                    },
                    "version": "one",
                }]}),
                codex_command_for_exec=lambda: "codex",
            )
            proc = SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(
                    b'{"decision":"prompt","matchedRules":[{}]}', b"",
                )),
                kill=Mock(),
            )
            probe = ExecPolicyProbe(
                appserver, Settings(codex_app_mode="internal")
            )
            with patch(
                "app.operations.asyncio.create_subprocess_exec",
                AsyncMock(return_value=proc),
            ):
                first = await probe.evaluate(["node", "--check", "app.js"], directory)
                rule_file.write_text(
                    'prefix_rule(pattern=["node"], decision="forbidden")',
                    encoding="utf-8",
                )
                second = await probe.evaluate(
                    ["node", "--check", "app.js"], directory
                )
            self.assertNotEqual(first.fingerprint, second.fingerprint)
