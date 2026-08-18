from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import sqlite3
import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.appserver.rpc_methods import CodexRpcMethods
from app.appserver.manager import AppServerManager
from app.appserver.isolated import IsolatedAppServer
from app.appserver.resolve import resolve_codex_executable
from app.appserver.ws_client import WsAppServerClient
from app.appserver.jsonrpc import JsonRpcError
from app.approval import ApprovalBridge, PendingRequest
from app.config import Settings
from app.db import Database
from app.models import ExecutionContext, ExecutionRegistry
from app.operations import OperationRouter
from app.oauth import Authenticator, WebAuthenticator
from app.native import (
    NativeRuntimeError,
    NativeRuntimeManager,
    _SameOriginAuthRedirectHandler,
    _extract_archive,
    _request,
)
from app.execution import (
    ExecutionError as SessionError,
    ExecutionOrchestrator as SessionOrchestrator,
    make_map_key,
)
from app.settings_store import SettingsStore
from app.tunnel.manager import ChatGptTunnel, CloudflaredTunnel, TunnelManager


class ApprovalContractTests(unittest.TestCase):
    def test_permission_response_uses_permission_contract(self):
        result = ApprovalBridge._to_response(
            "item/permissions/requestApproval",
            {"action": "always", "permissions": {"network": {"enabled": True}}},
        )
        self.assertEqual(result["scope"], "session")
        self.assertIn("permissions", result)
        self.assertNotIn("decision", result)

    def test_user_answers_are_arrays(self):
        result = ApprovalBridge._to_response(
            "item/tool/requestUserInput", {"action": "accept", "answers": {"q": "yes"}})
        self.assertEqual(result, {"answers": {"q": {"answers": ["yes"]}}})

    def test_accept_for_session_action_is_preserved(self):
        result = ApprovalBridge._to_response(
            "item/commandExecution/requestApproval", {"action": "acceptForSession"})
        self.assertEqual(result, {"decision": "acceptForSession"})


class TunnelContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloudflared_named_token_is_not_in_process_arguments(self):
        tunnel = CloudflaredTunnel(Settings(public_url="https://mcp.example.test"),
                                   mode="named", token="secret-tunnel-token")
        proc = SimpleNamespace(returncode=None, pid=43, stdout=None)
        with patch("app.tunnel.manager.shutil.which", return_value=r"C:\tools\cloudflared.exe"), \
             patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn, \
             patch.object(tunnel, "_attach_windows_kill_job") as attach:
            status = await tunnel.start()
        args = spawn.await_args.args
        env = spawn.await_args.kwargs["env"]
        self.assertNotIn("secret-tunnel-token", " ".join(args))
        self.assertNotIn("--token", args)
        self.assertEqual(env["TUNNEL_TOKEN"], "secret-tunnel-token")
        self.assertTrue(status["running"])
        attach.assert_called_once_with()

    async def test_trycloudflare_url_is_published_to_runtime_oauth(self):
        class Reader:
            def __init__(self):
                self.lines = iter([
                    b"INF route https://fresh-name.trycloudflare.com ready\n",
                    b"",
                ])

            async def readline(self):
                return next(self.lines)

        published = []
        tunnel = CloudflaredTunnel(
            Settings(), mode="try", on_public_url=published.append)
        tunnel.proc = SimpleNamespace(stdout=Reader())
        tunnel._url_ready = asyncio.Event()
        await tunnel._capture_try_url()
        self.assertEqual(tunnel.url, "https://fresh-name.trycloudflare.com")
        self.assertEqual(published, ["https://fresh-name.trycloudflare.com"])
        self.assertTrue(tunnel._url_ready.is_set())

    async def test_secure_tunnel_spawn_uses_secret_references_and_health_contract(self):
        class TestTunnel(ChatGptTunnel):
            async def _read_logs(self):
                return

            async def _monitor(self):
                return

            async def _wait_for_health_url(self):
                self.health_url = "http://127.0.0.1:9999"

            async def _probe(self):
                self.healthy = True
                self.ready = True

        settings = Settings(port=8123, mcp_auth_mode="token",
                            mcp_access_token="local-secret")
        tunnel = TestTunnel(settings, tunnel_id="tunnel_" + "a" * 32,
                            api_key="sk-runtime-secret", client_bin="tunnel-client.exe")
        tunnel._resolve_executable = lambda: r"C:\tools\tunnel-client.exe"
        proc = SimpleNamespace(returncode=None, pid=42, stdout=None)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn, \
             patch.object(tunnel, "_attach_windows_kill_job") as attach:
            status = await tunnel.start()
        args = spawn.await_args.args
        env = spawn.await_args.kwargs["env"]
        self.assertEqual(args[0], r"C:\tools\tunnel-client.exe")
        self.assertIn("--mcp.server-url", args)
        self.assertIn("http://127.0.0.1:8123/mcp/", args)
        self.assertIn("--health.url-file", args)
        self.assertNotIn("sk-runtime-secret", " ".join(args))
        self.assertEqual(env["CONTROL_PLANE_API_KEY"], "sk-runtime-secret")
        self.assertEqual(env["CHATCODEX_MCP_AUTH"], "Bearer local-secret")
        self.assertTrue(status["ready"])
        attach.assert_called_once_with()
        proc.returncode = 0
        await tunnel.stop()

    async def test_secure_tunnel_rejects_local_oauth_issuer_before_spawn(self):
        tunnel = ChatGptTunnel(
            Settings(mcp_auth_mode="oauth", public_url="http://127.0.0.1:8123"),
            tunnel_id="tunnel_" + "c" * 32,
            api_key="sk-runtime-secret",
            client_bin="tunnel-client.exe",
        )
        tunnel._resolve_executable = lambda: r"C:\tools\tunnel-client.exe"
        with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as spawn:
            status = await tunnel.start()
        self.assertFalse(status["running"])
        self.assertFalse(status["oauthCompatible"])
        self.assertIn("publicly reachable HTTPS issuer", status["detail"])
        spawn.assert_not_awaited()

    def test_secure_tunnel_accepts_public_https_oauth_issuer(self):
        tunnel = ChatGptTunnel(
            Settings(mcp_auth_mode="oauth", public_url="https://auth.example.test"),
            tunnel_id="tunnel_" + "d" * 32,
            api_key="sk-runtime-secret",
            client_bin="tunnel-client.exe",
        )
        status = tunnel.status()
        self.assertTrue(status["oauthCompatible"])
        self.assertEqual(status["oauthWarning"], "")

    def test_secure_tunnel_uses_environment_defaults(self):
        settings = Settings(chatgpt_tunnel_id="tunnel_" + "b" * 32,
                            chatgpt_api_key="sk-from-env")
        tunnel = TunnelManager(settings)._build("chatgpt")
        self.assertEqual(tunnel.tunnel_id, settings.chatgpt_tunnel_id)
        self.assertEqual(tunnel.api_key, settings.chatgpt_api_key)

    async def test_named_tunnels_are_thread_isolated(self):
        manager = TunnelManager(Settings(mcp_auth_mode="token"))
        try:
            first = await manager.start("direct", instance_id="project-a")
            second = await manager.start("direct", instance_id="project-b")
            self.assertTrue(first["threadIsolated"])
            self.assertNotEqual(first["threadName"], second["threadName"])
            status = manager.status("project-a")
            self.assertEqual(len(status["instances"]), 2)
            await manager.stop("project-a")
            self.assertEqual(
                [item["instanceId"] for item in manager.status()["instances"]],
                ["project-b"],
            )
        finally:
            await manager.stop()

    async def test_public_url_activation_runs_on_gateway_loop(self):
        caller_loop = asyncio.get_running_loop()
        observed = []
        manager = TunnelManager(
            Settings(mcp_auth_mode="token", public_url="https://mcp.example.test"),
            on_public_url=lambda value: observed.append(
                (value, asyncio.get_running_loop())),
        )
        try:
            await manager.start("direct", instance_id="public-route")
            self.assertEqual(observed, [
                ("https://mcp.example.test", caller_loop),
            ])
        finally:
            await manager.stop()

    async def test_explicit_instance_status_does_not_fall_back_to_other_transport(self):
        manager = TunnelManager(Settings(mcp_auth_mode="token"))
        try:
            await manager.start("direct", instance_id="public-route")
            missing = manager.status("chatgpt-mcp")
            self.assertEqual(missing["kind"], "none")
            self.assertFalse(missing["running"])
            self.assertEqual(len(missing["instances"]), 1)
        finally:
            await manager.stop()


class SecurityContractTests(unittest.TestCase):
    def test_cookie_authenticated_admin_writes_require_same_origin(self):
        from fastapi import HTTPException
        from app.main import _WEB_COOKIE, settings, web_principal

        def request(origin: str):
            return SimpleNamespace(
                method="POST",
                headers={"origin": origin, "host": "gateway.example.test"},
                cookies={_WEB_COOKIE: settings.web_access_token},
                url=SimpleNamespace(scheme="https"),
            )

        self.assertEqual(
            web_principal(None, request("https://gateway.example.test")).user_id,
            "web-admin",
        )
        with self.assertRaises(HTTPException) as rejected:
            web_principal(None, request("https://evil.example.test"))
        self.assertEqual(rejected.exception.status_code, 403)

    def test_default_database_migrates_out_of_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = os.path.join(directory, "legacy.db")
            target = os.path.join(directory, "private", "chatcodex.db")
            connection = sqlite3.connect(legacy)
            try:
                connection.execute("CREATE TABLE kv_config (key TEXT PRIMARY KEY, value TEXT)")
                connection.execute(
                    "INSERT INTO kv_config(key,value) VALUES(?,?)",
                    ("set:web_access_token", '"secret"'),
                )
                connection.commit()
            finally:
                connection.close()
            with patch("app.db._legacy_database_path", return_value=legacy), \
                 patch("app.db._default_database_path", return_value=target):
                db = Database(Settings(database_url=f"sqlite:///{target}"))
                try:
                    with db.conn() as connection:
                        row = connection.execute(
                            "SELECT value FROM kv_config WHERE key=?",
                            ("set:web_access_token",),
                        ).fetchone()
                    self.assertEqual(row["value"], '"secret"')
                    self.assertFalse(os.path.exists(legacy))
                finally:
                    db.close()

    def test_noauth_mcp_cannot_bind_non_loopback(self):
        from app.mcp_server import _transport_security

        with self.assertRaisesRegex(ValueError, "only bind to a loopback"):
            _transport_security(Settings(mcp_auth_mode="noauth", host="0.0.0.0"))
        local = _transport_security(
            Settings(mcp_auth_mode="noauth", host="127.0.0.1"))
        self.assertTrue(local.enable_dns_rebinding_protection)

    def test_conversation_map_key_is_scoped_to_authenticated_principal(self):
        meta = {"openai/session": "same-conversation"}
        self.assertNotEqual(
            make_map_key(meta, "oauth-client-a"),
            make_map_key(meta, "oauth-client-b"),
        )

    def test_web_and_mcp_tokens_are_isolated(self):
        settings = Settings(web_access_token="web-secret", mcp_auth_mode="token",
                            mcp_access_token="mcp-secret")
        web = WebAuthenticator(settings.web_access_token)
        mcp = Authenticator(settings)
        self.assertIsNotNone(web.authenticate("web-secret"))
        self.assertIsNone(web.authenticate("mcp-secret"))
        self.assertIsNotNone(mcp.authenticate("Bearer mcp-secret", "127.0.0.1"))
        self.assertIsNone(mcp.authenticate("Bearer web-secret", "127.0.0.1"))

    def test_mcp_both_accepts_static_and_oauth_tokens(self):
        settings = Settings(
            mcp_auth_mode="both", mcp_access_token="mcp-secret",
            oauth_password="pw", oauth_token_secret="a-long-random-test-secret",
            public_url="https://example.test",
        )
        auth = Authenticator(settings)
        oauth_token = auth.signer.issue("user", ["codex"])
        self.assertEqual(
            auth.authenticate("Bearer mcp-secret", "127.0.0.1").user_id,
            "mcp-token",
        )
        self.assertEqual(
            auth.authenticate(f"Bearer {oauth_token}", "127.0.0.1").user_id,
            "user",
        )

    def test_new_appserver_instance_invalidates_active_execution_contexts(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Settings(database_url=f"sqlite:///{directory}/test.db"))
            registry = ExecutionRegistry(db)
            context = registry.configure(
                map_key="map",
                conversation_id="conversation",
                user_id="user",
                cwd=directory,
                workspace_roots=[directory],
                sandbox_mode="workspace-write",
                permission_profile_id=None,
                approval_policy="on-request",
                work_mode="agent",
                appserver_instance_id="instance-1",
            )
            self.assertEqual(registry.invalidate_appserver_instance(), 1)
            invalidated = registry.get(context.id)
            self.assertEqual(invalidated.status, "active")
            self.assertEqual(invalidated.version, context.version + 1)
            self.assertEqual(invalidated.appserver_instance_id, "")
            db.close()

    def test_paths_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            sm = ExecutionContext(
                id="1", map_key="m", conversation_id="c", user_id="u",
                cwd=directory,
                workspace_roots=json.dumps([directory]),
            )
            orch = SessionOrchestrator(Settings(), object(), object())
            self.assertEqual(
                orch._resolve(sm, "backend"),
                os.path.realpath(os.path.join(directory, "backend")),
            )
            with self.assertRaises(SessionError):
                orch._resolve(sm, os.path.join(directory, os.pardir, "outside"))

    def test_patch_headers_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            sm = ExecutionContext(
                id="1", map_key="m", conversation_id="c", user_id="u",
                cwd=directory,
                workspace_roots=json.dumps([directory]),
            )
            orch = SessionOrchestrator(Settings(), object(), object())
            with self.assertRaises(SessionError):
                orch._validate_patch_paths(sm, """*** Begin Patch
*** Update File: ../outside.txt
@@
-a
+b
*** End Patch""")

    def test_oauth_tokens_are_issuer_and_audience_bound(self):
        settings = Settings(mcp_auth_mode="oauth", oauth_password="pw",
                            oauth_token_secret="a-long-random-test-secret",
                            public_url="https://example.test")
        auth = Authenticator(settings)
        token = auth.signer.issue("user", ["codex"])
        self.assertIsNotNone(auth.signer.verify(token))
        other = Authenticator(Settings(mcp_auth_mode="oauth", oauth_password="pw",
                                      oauth_token_secret="a-long-random-test-secret",
                                      public_url="https://other.test"))
        self.assertIsNone(other.signer.verify(token))

    def test_oauth_runtime_public_url_updates_issuer_and_resource(self):
        auth = Authenticator(Settings(
            mcp_auth_mode="oauth",
            oauth_token_secret="a-long-random-test-secret",
            public_url="http://127.0.0.1:8000",
        ))
        auth.set_public_url("https://fresh-name.trycloudflare.com")
        self.assertEqual(
            auth.public_url, "https://fresh-name.trycloudflare.com")
        self.assertEqual(
            auth.resource, "https://fresh-name.trycloudflare.com/mcp")
        token = auth.signer.issue("user", ["codex"])
        self.assertIsNotNone(auth.authenticate(f"Bearer {token}"))

    def test_oauth_accepts_only_configured_openai_tunnel_resource(self):
        tunnel_id = "tunnel_" + "a" * 32
        auth = Authenticator(Settings(
            mcp_auth_mode="oauth", oauth_token_secret="a-long-random-test-secret",
            public_url="https://gateway.example.test",
            chatgpt_tunnel_id=tunnel_id,
        ))
        resource = (
            "https://tunnel-service.gateway.unified-0.internal.api.openai.org"
            f"/v1/mcp/{tunnel_id}"
        )
        self.assertTrue(auth.accepts_resource(resource))
        token = auth.signer.issue("user", ["codex"], audience=resource)
        self.assertIsNotNone(auth.authenticate(f"Bearer {token}"))
        self.assertFalse(auth.accepts_resource(
            f"https://attacker.example/v1/mcp/{tunnel_id}"))
        self.assertFalse(auth.accepts_resource(
            "https://tunnel-service.gateway.unified-0.internal.api.openai.org"
            f"/v1/mcp/tunnel_{'b' * 32}"))

    def test_dynamic_registration_cannot_override_client_id(self):
        auth = Authenticator(Settings(public_url="https://example.test"))
        client = auth.store.register_client({
            "client_id": "attacker-chosen",
            "redirect_uris": ["https://chatgpt.com/aip/callback"],
        })
        self.assertNotEqual(client["client_id"], "attacker-chosen")
        with self.assertRaises(ValueError):
            auth.store.register_client({"redirect_uris": ["http://evil.example/callback"]})

    def test_dynamic_registration_rejects_ambiguous_redirects_and_large_metadata(self):
        auth = Authenticator(Settings(public_url="https://example.test"))
        for uri in (
                "https://chatgpt.com/connector/oauth/callback#fragment",
                "https://user:password@chatgpt.com/connector/oauth/callback"):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                auth.store.register_client({"redirect_uris": [uri]})
        with self.assertRaisesRegex(ValueError, "too large"):
            auth.store.register_client({
                "redirect_uris": ["https://chatgpt.com/connector/oauth/callback"],
                "client_name": "x" * (33 * 1024),
            })

    def test_oauth_access_token_preserves_client_identity(self):
        auth = Authenticator(Settings(
            mcp_auth_mode="oauth", oauth_token_secret="a-long-random-test-secret",
            public_url="https://example.test",
        ))
        token = auth.signer.issue("user", ["codex"], client_id="client-a")
        principal = auth.authenticate(f"Bearer {token}", "127.0.0.1")
        self.assertEqual(principal.client_id, "client-a")

    def test_dynamic_registration_accepts_chatgpt_refresh_grant(self):
        auth = Authenticator(Settings(public_url="https://example.test"))
        client = auth.store.register_client({
            "redirect_uris": [
                "https://chatgpt.com/connector/oauth/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        })
        self.assertEqual(
            client["grant_types"], ["authorization_code", "refresh_token"])
        with self.assertRaisesRegex(ValueError, "authorization_code"):
            auth.store.register_client({
                "redirect_uris": [
                    "https://chatgpt.com/connector/oauth/callback"],
                "grant_types": ["client_credentials"],
            })

    def test_refresh_token_cannot_authenticate_mcp_requests(self):
        auth = Authenticator(Settings(
            mcp_auth_mode="oauth",
            oauth_token_secret="a-long-random-test-secret",
            public_url="https://example.test",
        ))
        refresh_token = auth.signer.issue(
            "user", ["codex"], client_id="client-a", token_use="refresh")
        self.assertIsNone(auth.authenticate(
            f"Bearer {refresh_token}", "127.0.0.1"))
        principal = auth.signer.verify_refresh(refresh_token)
        self.assertEqual(principal.client_id, "client-a")

    def test_dynamic_registration_cache_is_bounded(self):
        auth = Authenticator(Settings(public_url="https://example.test"))
        clients = []
        with patch("app.oauth.MAX_OAUTH_CLIENTS", 2):
            for index in range(3):
                clients.append(auth.store.register_client({
                    "redirect_uris": [
                        f"https://chatgpt.com/connector/oauth/callback-{index}"],
                }))
        self.assertNotIn(clients[0]["client_id"], auth.store.clients)
        self.assertIn(clients[-1]["client_id"], auth.store.clients)

    def test_oauth_authorization_code_cache_and_verifier_are_bounded(self):
        auth = Authenticator(Settings(public_url="https://example.test"))
        codes = []
        with patch("app.oauth.MAX_OAUTH_CODES", 2):
            for index in range(3):
                codes.append(auth.store.issue_code(
                    "client", "https://chatgpt.com/connector/oauth/callback",
                    "a" * 43, "S256", "user", "codex",
                    "https://example.test/mcp",
                ))
        self.assertNotIn(codes[0], auth.store.codes)
        self.assertIn(codes[-1], auth.store.codes)
        self.assertIsNone(auth.store.redeem_code(
            codes[-1], "client",
            "https://chatgpt.com/connector/oauth/callback", "short"))
        self.assertIn(codes[-1], auth.store.codes)

    def test_dynamic_registration_survives_gateway_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                database_url=f"sqlite:///{directory}/oauth.db",
                public_url="https://gateway.example.test",
            )
            database = Database(settings)
            first = Authenticator(settings, db=database)
            client = first.store.register_client({
                "redirect_uris": [
                    "https://chatgpt.com/connector/oauth/callback-123"],
            })
            second = Authenticator(settings, db=database)
            self.assertEqual(
                second.store.get_client(client["client_id"]), client)
            database.close()

    def test_oauth_callback_protection_only_accepts_chatgpt_connector(self):
        auth = Authenticator(Settings(oauth_callback_protection=True))
        with self.assertRaises(ValueError):
            auth.store.register_client({"redirect_uris": ["http://localhost/callback"]})
        client = auth.store.register_client({
            "redirect_uris": ["https://chatgpt.com/connector/oauth/callback-123"],
        })
        self.assertEqual(
            client["redirect_uris"],
            ["https://chatgpt.com/connector/oauth/callback-123"],
        )

    def test_native_runtime_rejects_foreign_codex_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = os.path.join(
                directory, "codex-package-aarch64-unknown-linux-musl.zip")
            import zipfile
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("codex/bin/codex", b"foreign")
            runtime = NativeRuntimeManager(os.path.join(directory, "native"))
            if runtime.codex_target != "aarch64-unknown-linux-musl":
                with self.assertRaises(NativeRuntimeError):
                    runtime.install_codex(repository="owner/repo", archive_path=archive)

    def test_native_runtime_rejects_archive_path_traversal(self):
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            archive = os.path.join(directory, "payload.zip")
            destination = os.path.join(directory, "destination")
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("../escape.txt", b"escape")
            with self.assertRaises(NativeRuntimeError):
                _extract_archive(Path(archive), Path(destination))
            self.assertFalse(os.path.exists(os.path.join(directory, "escape.txt")))

    def test_native_runtime_selects_exact_official_release_package(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = NativeRuntimeManager(os.path.join(directory, "native"))
            expected_name = f"codex-package-{runtime.codex_target}.tar.gz"
            expected_url = f"https://downloads.example/{expected_name}"
            release = {"assets": [
                {"name": "codex.exe", "browser_download_url": "https://wrong.example"},
                {"name": expected_name, "browser_download_url": expected_url},
            ]}
            with patch.object(
                    runtime, "_read_url",
                    return_value=json.dumps(release).encode("utf-8")) as fetch:
                self.assertEqual(
                    runtime._latest_release_asset("openai/codex", ""), expected_url)
            self.assertIn("/repos/openai/codex/releases/latest", fetch.call_args.args[0])

    def test_github_release_metadata_uses_json_media_type(self):
        metadata = _request("https://api.github.com/repos/openai/codex/releases/latest")
        binary = _request("https://github.com/openai/codex/releases/download/v1/file.tar.gz")
        self.assertEqual(metadata.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(binary.get_header("Accept"), "application/octet-stream")

    def test_github_token_is_not_forwarded_to_lookalike_hosts(self):
        trusted = _request("https://api.github.com/repos/openai/codex", "secret")
        lookalike = _request("https://github.com.attacker.example/file", "secret")
        self.assertEqual(trusted.get_header("Authorization"), "Bearer secret")
        self.assertIsNone(lookalike.get_header("Authorization"))

    def test_native_download_redirect_strips_cross_origin_authorization(self):
        original = _request(
            "https://github.com/openai/codex/releases/download/v1/a.zip", "secret")
        redirected = _SameOriginAuthRedirectHandler().redirect_request(
            original, None, 302, "Found", {},
            "https://downloads.example.test/a.zip",
        )
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_native_runtime_download_rejects_plain_http(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = NativeRuntimeManager(directory)
            with self.assertRaisesRegex(NativeRuntimeError, "HTTPS"):
                runtime._download("http://example.test/codex.zip", "codex.zip")

    def test_legacy_codexext_repository_setting_migrates_to_official(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Settings(database_url=f"sqlite:///{directory}/settings.db"))
            store = SettingsStore(db)
            store.set("codex_release_repo", "AeroidesLab/codexext")
            self.assertEqual(store.get("codex_release_repo"), "openai/codex")
            self.assertEqual(store.get_override("codex_release_repo"), "openai/codex")
            db.close()


class AppServerCapabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_absolute_npm_cmd_resolves_to_native_vendor_binary(self):
        shim = os.path.join(
            os.environ.get("APPDATA", r"C:\Users\test\AppData\Roaming"),
            "npm", "codex.CMD")
        with patch("app.appserver.resolve.sys.platform", "win32"), \
             patch("app.appserver.resolve.os.path.isabs", return_value=True), \
             patch("app.appserver.resolve.os.path.isfile", return_value=True), \
             patch("app.appserver.resolve.Path.exists", return_value=True):
            command = resolve_codex_executable(shim)
        self.assertEqual(len(command), 1)
        self.assertTrue(command[0].lower().endswith("codex.exe"))

    async def test_official_api_probe_only_treats_method_not_found_as_unsupported(self):
        client = WsAppServerClient(Settings())

        async def probe(method, params, *, timeout=120.0):
            if method == "unsupported/example":
                raise JsonRpcError(-32601, "method not found")
            raise JsonRpcError(-32000, "invalid probe parameters")

        client.call = AsyncMock(side_effect=probe)
        capabilities = await client._probe_official_api()
        self.assertEqual(capabilities, {
            "commandExec": True,
            "configRead": True,
            "configRequirements": True,
            "permissionProfiles": True,
        })

    async def test_start_failure_is_reported_for_first_run_admin_recovery(self):
        manager = AppServerManager(Settings())
        manager._spawn = AsyncMock(side_effect=RuntimeError("runtime missing"))
        with self.assertRaises(RuntimeError):
            await manager.start()
        self.assertFalse(manager.status()["healthy"])
        self.assertIn("runtime missing", manager.status()["lastError"])

    async def test_spawn_failure_closes_partial_server_and_removes_token_file(self):
        with tempfile.TemporaryDirectory() as root:
            token_file = os.path.join(root, "app-server.token")
            with open(token_file, "w", encoding="utf-8") as stream:
                stream.write("secret\n")
            native = SimpleNamespace(
                internal_token_file=Mock(return_value=token_file),
                codex_command=Mock(return_value=""),
            )
            server = SimpleNamespace(
                on_server_request=Mock(),
                on_notification=Mock(),
                start=AsyncMock(side_effect=RuntimeError("handshake failed")),
                close=AsyncMock(),
            )
            manager = AppServerManager(
                Settings(codex_command=os.__file__), native=native)
            with patch("app.appserver.manager.IsolatedAppServer", return_value=server):
                with self.assertRaisesRegex(RuntimeError, "handshake failed"):
                    await manager._spawn()
            server.close.assert_awaited_once()
            self.assertIsNone(manager._server)
            self.assertFalse(os.path.exists(token_file))

    async def test_concurrent_appserver_start_spawns_once(self):
        manager = AppServerManager(Settings())
        manager._spawn = AsyncMock()
        await asyncio.gather(manager.start(), manager.start())
        manager._spawn.assert_awaited_once()
        await manager.stop()

    async def test_external_appserver_requires_tls_outside_loopback(self):
        manager = AppServerManager(Settings(
            codex_app_mode="external",
            codex_external_ws_url="ws://example.test:8765",
            codex_external_ws_key="secret",
        ))
        with self.assertRaisesRegex(ValueError, "must use wss"):
            await manager._spawn()

    async def test_external_appserver_rejects_url_credentials(self):
        manager = AppServerManager(Settings(
            codex_app_mode="external",
            codex_external_ws_url="wss://user:password@example.test/app-server",
        ))
        with self.assertRaisesRegex(ValueError, "must use ws"):
            await manager._spawn()

    def test_appserver_refuses_to_attach_to_an_occupied_port(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            with self.assertRaises(OSError):
                WsAppServerClient(Settings(), port=port)._ensure_port_available()

    def test_secure_tunnel_uses_loopback_target_for_wildcard_bind(self):
        tunnel = ChatGptTunnel(Settings(host="0.0.0.0", port=8123), client_bin="missing")
        self.assertEqual(tunnel._target(), "http://127.0.0.1:8123")

    def test_noauth_mcp_transport_only_accepts_loopback_hosts(self):
        from app.mcp_server import _transport_security

        transport = _transport_security(Settings(mcp_auth_mode="noauth"))
        self.assertTrue(transport.enable_dns_rebinding_protection)
        self.assertIn("127.0.0.1:*", transport.allowed_hosts)
        self.assertNotIn("example.com:*", transport.allowed_hosts)

    def test_authenticated_mcp_transport_allows_tunnel_hosts(self):
        from app.mcp_server import _transport_security

        transport = _transport_security(Settings(mcp_auth_mode="token"))
        self.assertFalse(transport.enable_dns_rebinding_protection)

    def test_plan_mode_defaults_are_separate_from_explicit_safety_axes(self):
        self.assertEqual(
            OperationRouter.owner_for("command/exec"),
            "gateway",
        )
        self.assertEqual(
            OperationRouter.owner_for("config/read"),
            "none",
        )

    def test_explicit_full_access_axes_override_stored_agent_defaults(self):
        self.assertEqual(
            OperationRouter.owner_for("unknown/mutation"), "forbidden")


class DiscoveryContractTests(unittest.TestCase):
    def test_subagent_tools_are_unconditionally_absent(self):
        from app import main

        names = set(main.mcp._tool_manager._tools)
        self.assertTrue({
            "spawn_agent", "send_input", "send_message", "followup_task",
            "resume_agent", "wait_agent", "list_agents", "interrupt_agent",
            "close_agent",
        }.isdisjoint(names))

    def test_chatgpt_security_schemes_only_advertise_supported_types(self):
        from app.mcp_server import tool_security_schemes

        self.assertEqual(
            tool_security_schemes(Settings(mcp_auth_mode="both")),
            [{"type": "oauth2", "scopes": ["codex"]}],
        )
        self.assertEqual(
            tool_security_schemes(Settings(mcp_auth_mode="token")),
            [{"type": "noauth"}],
        )


    def test_oauth_protected_resource_metadata_supports_mcp_path(self):
        from app.main import (
            _authorization_server_metadata,
            _consent_page,
            _oauth_consent_response,
            _oauth_redirect_origin,
            _protected_resource_metadata,
            app,
        )

        paths = {route.path for route in app.routes}
        self.assertIn("/.well-known/oauth-protected-resource", paths)
        self.assertIn("/.well-known/oauth-protected-resource/mcp", paths)
        self.assertNotIn("/api/config", paths)
        protected = _protected_resource_metadata()
        server = _authorization_server_metadata()
        self.assertEqual(protected["authorization_servers"], [server["issuer"]])

        consent = _consent_page(
            "client-123", "codex",
            '<input type=hidden name=state value="state-1">', True,
        )
        self.assertIn("应用:<b>client-123</b>", consent)
        self.assertIn("权限范围:codex", consent)
        self.assertIn('name=state value="state-1"', consent)
        self.assertIn("输入访问密码", consent)
        response = _oauth_consent_response(
            consent,
            "https://chatgpt.com/connector/oauth/callback-123",
        )
        self.assertIn(
            "form-action 'self' https://chatgpt.com;",
            response.headers["content-security-policy"],
        )
        self.assertEqual(
            _oauth_redirect_origin("http://localhost:8765/callback"),
            "http://localhost:8765",
        )
        self.assertEqual(
            _oauth_redirect_origin("https://user:password@example.test/callback"),
            "",
        )
        for placeholder in (
                "{client}", "{scope}", "{hidden}",
                "{password_field}", "{error}"):
            self.assertNotIn(placeholder, consent)
        self.assertEqual(server["token_endpoint_auth_methods_supported"], ["none"])
        self.assertEqual(server["code_challenge_methods_supported"], ["S256"])
        self.assertTrue(server["registration_endpoint"].endswith("/oauth/register"))

    def test_tools_and_widget_resources_have_apps_contracts(self):
        from app.main import mcp, update_widget_domains

        update_widget_domains(mcp, "https://chatcodex.example.test")

        names = set(mcp._tool_manager._tools)
        model_tools = {
            "open_workspace_setup",
            "exec_command", "apply_patch", "read_file", "write_file",
            "list_dir", "search_files", "update_plan", "view_image",
            "request_user_input",
        }
        app_only_tools = {
            "save_execution_context", "execution_status",
            "resolve_approval", "browse_dir",
        }
        self.assertTrue(model_tools.issubset(names))
        self.assertTrue(app_only_tools.issubset(names))
        self.assertNotIn("codex", names)
        self.assertNotIn("web_search", names)
        self.assertIn("save_execution_context", names)
        for name in app_only_tools:
            self.assertEqual(
                mcp._tool_manager._tools[name].meta["ui"]["visibility"],
                ["app"],
                name,
            )
        for name in model_tools:
            visibility = ((mcp._tool_manager._tools[name].meta or {}).get("ui") or {}).get("visibility")
            self.assertNotEqual(visibility, ["app"], name)
        self.assertNotIn(
            "config",
            mcp._tool_manager._tools[
                "open_workspace_setup"
            ].parameters.get("properties", {}),
        )
        self.assertTrue(mcp._tool_manager._tools["write_file"].annotations.destructiveHint)
        self.assertTrue(mcp._tool_manager._tools["exec_command"].annotations.destructiveHint)
        self.assertTrue(mcp._tool_manager._tools["apply_patch"].annotations.destructiveHint)
        self.assertEqual(
            mcp._tool_manager._tools["apply_patch"].meta["ui"]["resourceUri"],
            "ui://widget/diff.html",
        )
        self.assertEqual(
            mcp._tool_manager._tools["apply_patch"].meta["openai/outputTemplate"],
            "ui://widget/diff.html",
        )
        self.assertEqual(
            mcp._tool_manager._tools["write_file"].meta["ui"]["resourceUri"],
            "ui://widget/diff.html",
        )
        self.assertEqual(
            mcp._tool_manager._tools["write_file"].meta["openai/outputTemplate"],
            "ui://widget/diff.html",
        )
        for tool in mcp._tool_manager._tools.values():
            self.assertTrue(tool.title)
            self.assertIsNotNone(tool.output_schema)
            self.assertIsNotNone(tool.annotations)
            self.assertIsNotNone(tool.annotations.readOnlyHint)
            self.assertIsNotNone(tool.annotations.destructiveHint)
            self.assertIsNotNone(tool.annotations.idempotentHint)
            self.assertIsNotNone(tool.annotations.openWorldHint)
            self.assertIn("securitySchemes", tool.meta or {})
        for resource in mcp._resource_manager._resources.values():
            self.assertEqual(resource.mime_type, "text/html;profile=mcp-app")
            self.assertIn("ui", resource.meta)
            self.assertFalse(resource.meta["ui"]["prefersBorder"])
            self.assertEqual(
                resource.meta["ui"]["domain"],
                "https://chatcodex.example.test",
            )
            self.assertEqual(
                resource.meta["openai/widgetDomain"],
                "https://chatcodex.example.test",
            )
        self.assertEqual(
            set(mcp._resource_manager._resources),
            {"ui://widget/workspace-setup.html", "ui://widget/chat.html",
             "ui://widget/ask-user.html", "ui://widget/approval.html",
             "ui://widget/diff.html"},
        )
        self.assertEqual(
            mcp._tool_manager._tools["request_user_input"].meta["ui"]["resourceUri"],
            "ui://widget/ask-user.html",
        )

    def test_workspace_default_does_not_resolve_the_reference_fork(self):
        with patch.dict(os.environ, {"CHATCODEX_CODEX_COMMAND": ""}, clear=False):
            command = Settings().codex_command.replace("/", "\\").lower()
        self.assertNotIn("\\ref\\codex\\codex-rs\\target\\", command)


if __name__ == "__main__":
    unittest.main()
