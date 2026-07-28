"""MCP 认证:静态 Token + OAuth 2.1(PKCE S256,stdlib 实现)。

无第三方 JWT 库:用 hmac 自签 HS256 token。模式:
  token  — MCP Access Token 直接比对
  oauth  — Authorization Code + PKCE;/.well-known/* + /oauth/register|authorize|token
  both   — 同时接受 MCP Access Token 和 OAuth access token
  noauth — 仅 loopback
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from .config import Settings


MAX_OAUTH_CLIENTS = 512
MAX_OAUTH_CODES = 1024
MAX_CLIENT_METADATA_BYTES = 32 * 1024
MAX_REDIRECT_URIS = 10


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@dataclass
class Principal:
    user_id: str
    scopes: list[str]
    client_id: str = ""
    audience: str = ""


class TokenSigner:
    """HS256 自签 token(JSON header.payload.sig)。"""

    def __init__(self, secret: str, ttl: int, issuer: str, audience: str):
        self.secret = secret.encode()
        self.ttl = ttl
        self.issuer = issuer.rstrip("/")
        self.audience = audience

    def set_issuer(self, issuer: str, audience: str) -> None:
        """Switch future token issuance and verification to a runtime URL."""
        self.issuer = issuer.rstrip("/")
        self.audience = audience

    def issue(self, subject: str, scopes: Optional[list[str]] = None,
              audience: Optional[str] = None, client_id: str = "",
              token_use: str = "access", ttl: Optional[int] = None) -> str:
        now = int(time.time())
        header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        payload = _b64url(json.dumps({
            "sub": subject, "iat": now,
            "exp": now + (self.ttl if ttl is None else ttl),
            "iss": self.issuer, "aud": audience or self.audience,
            "scope": " ".join(scopes or ["codex"]),
            "client_id": client_id,
            "token_use": token_use,
        }, separators=(",", ":")).encode())
        sig = _b64url(hmac.new(self.secret, f"{header}.{payload}".encode(), hashlib.sha256).digest())
        return f"{header}.{payload}.{sig}"

    def verify(self, token: str,
               audience_validator: Optional[Callable[[str], bool]] = None
               ) -> Optional[Principal]:
        return self._verify(token, "access", audience_validator)

    def verify_refresh(self, token: str,
                       audience_validator: Optional[Callable[[str], bool]] = None
                       ) -> Optional[Principal]:
        """Verify a refresh token without allowing it as an MCP access token."""
        return self._verify(token, "refresh", audience_validator)

    def _verify(self, token: str, expected_use: str,
                audience_validator: Optional[Callable[[str], bool]]
                ) -> Optional[Principal]:
        try:
            header, payload, sig = token.split(".")
            if json.loads(_b64url_decode(header)).get("alg") != "HS256":
                return None
            expect = _b64url(hmac.new(self.secret, f"{header}.{payload}".encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(expect, sig):
                return None
            data = json.loads(_b64url_decode(payload))
            if data.get("exp", 0) <= int(time.time()):
                return None
            # Tokens issued before token_use was added are access tokens.
            if str(data.get("token_use") or "access") != expected_use:
                return None
            audience = data.get("aud", "")
            audience_ok = (audience_validator(audience) if audience_validator
                           else audience == self.audience)
            if data.get("iss") != self.issuer or not audience_ok:
                return None
            return Principal(user_id=data.get("sub", "unknown"),
                             scopes=(data.get("scope", "").split()),
                             client_id=str(data.get("client_id") or ""),
                             audience=str(audience))
        except Exception:
            return None


class OAuthStore:
    """Short-lived codes in memory; DCR clients optionally persisted in sqlite."""

    def __init__(self, callback_protection: bool = False, db: Any = None):
        self.clients: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.callback_protection = callback_protection
        self.db = db

    def get_client(self, client_id: str) -> Optional[dict]:
        client = self.clients.get(client_id)
        if client is not None or self.db is None or not client_id:
            return client
        with self.db.conn() as connection:
            row = connection.execute(
                "SELECT value FROM kv_config WHERE key=?",
                (f"oauth-client:{client_id}",),
            ).fetchone()
        if not row:
            return None
        try:
            client = json.loads(row["value"])
        except Exception:
            return None
        if not isinstance(client, dict) or client.get("client_id") != client_id:
            return None
        self.clients[client_id] = client
        return client

    def register_client(self, meta: dict) -> dict:
        if not isinstance(meta, dict):
            raise ValueError("client metadata must be an object")
        if len(json.dumps(meta, ensure_ascii=False).encode("utf-8")) > MAX_CLIENT_METADATA_BYTES:
            raise ValueError("client metadata is too large")
        redirect_uris = meta.get("redirect_uris")
        if (not isinstance(redirect_uris, list) or not redirect_uris
                or len(redirect_uris) > MAX_REDIRECT_URIS):
            raise ValueError("redirect_uris is required")
        if not all(isinstance(uri, str) and len(uri) <= 2048
                   and _valid_redirect_uri(uri) for uri in redirect_uris):
            raise ValueError("redirect_uris must use HTTPS (localhost HTTP is allowed)")
        if self.callback_protection and not all(
                is_chatgpt_connector_callback(str(uri)) for uri in redirect_uris):
            raise ValueError(
                "redirect_uris are restricted to https://chatgpt.com/connector/oauth/*")
        if meta.get("token_endpoint_auth_method", "none") != "none":
            raise ValueError("only public PKCE clients are supported")
        grant_types = meta.get("grant_types", ["authorization_code"])
        if (not isinstance(grant_types, list)
                or not all(isinstance(grant, str) for grant in grant_types)
                or len(grant_types) != len(set(grant_types))
                or "authorization_code" not in grant_types
                or not set(grant_types).issubset(
                    {"authorization_code", "refresh_token"})):
            raise ValueError(
                "grant_types must include authorization_code and may include refresh_token")
        response_types = meta.get("response_types", ["code"])
        if (not isinstance(response_types, list)
                or set(response_types) != {"code"}
                or len(response_types) != 1):
            raise ValueError("response_types must be ['code']")
        if ("scope" in meta and
                (not isinstance(meta.get("scope"), str)
                 or not set(meta["scope"].split())
                 or not set(meta["scope"].split()).issubset({"codex"}))):
            raise ValueError("scope must contain only codex")
        cid = secrets.token_hex(16)
        allowed = {k: meta[k] for k in (
            "redirect_uris", "client_name", "client_uri", "logo_uri",
            "scope", "token_endpoint_auth_method",
        ) if k in meta}
        rec = {**allowed, "grant_types": grant_types,
               "response_types": response_types,
               "client_id": cid, "client_id_issued_at": int(time.time()),
               "token_endpoint_auth_method": "none"}
        # 公共客户端(PKCE)无 secret
        while len(self.clients) >= MAX_OAUTH_CLIENTS:
            oldest = min(
                self.clients,
                key=lambda key: int(
                    self.clients[key].get("client_id_issued_at") or 0),
            )
            self.clients.pop(oldest, None)
        self.clients[cid] = rec
        if self.db is not None:
            with self.db.conn() as connection:
                rows = connection.execute(
                    "SELECT key FROM kv_config WHERE key LIKE 'oauth-client:%' "
                    "ORDER BY rowid ASC"
                ).fetchall()
                excess = max(0, len(rows) - MAX_OAUTH_CLIENTS + 1)
                for row in rows[:excess]:
                    old_key = row["key"]
                    connection.execute("DELETE FROM kv_config WHERE key=?", (old_key,))
                    self.clients.pop(old_key.removeprefix("oauth-client:"), None)
                connection.execute(
                    "INSERT OR REPLACE INTO kv_config(key,value) VALUES(?,?)",
                    (f"oauth-client:{cid}", json.dumps(rec, separators=(",", ":"))),
                )
        return rec

    def issue_code(self, client_id: str, redirect_uri: str, challenge: str,
                   method: str, user_id: str, scope: str, resource: str) -> str:
        now = int(time.time())
        self.codes = {
            key: value for key, value in self.codes.items()
            if int(value.get("exp") or 0) >= now
        }
        while len(self.codes) >= MAX_OAUTH_CODES:
            oldest = min(
                self.codes,
                key=lambda key: int(self.codes[key].get("issued_at") or 0),
            )
            self.codes.pop(oldest, None)
        code = secrets.token_urlsafe(32)
        self.codes[code] = {
            "client_id": client_id, "redirect_uri": redirect_uri,
            "code_challenge": challenge, "code_challenge_method": method,
            "user_id": user_id, "scope": scope, "issued_at": now, "exp": now + 600,
            "resource": resource,
        }
        return code

    def redeem_code(self, code: str, client_id: str, redirect_uri: str, verifier: str) -> Optional[dict]:
        if not all(isinstance(value, str) for value in (
                code, client_id, redirect_uri, verifier)):
            return None
        if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
            return None
        rec = self.codes.pop(code, None)
        if not rec or rec["exp"] < int(time.time()):
            return None
        if rec["client_id"] != client_id or rec["redirect_uri"] != redirect_uri:
            return None
        # PKCE S256 校验
        digest = _b64url(hashlib.sha256(verifier.encode()).digest())
        if not hmac.compare_digest(digest, rec["code_challenge"]):
            return None
        return rec


class Authenticator:
    def __init__(self, settings: Settings, db: Any = None):
        self.settings = settings
        self.mode = settings.mcp_auth_mode
        self.public_url = settings.public_url.rstrip("/")
        self.resource = f"{self.public_url}/mcp"
        self.chatgpt_tunnel_id = settings.chatgpt_tunnel_id
        self.signer = TokenSigner(settings.oauth_token_secret, settings.oauth_token_ttl,
                                  self.public_url, self.resource)
        self.store = OAuthStore(settings.oauth_callback_protection, db=db)

    def set_public_url(self, public_url: str) -> None:
        """Update the issuer/resource used by the current running instance.

        This is used by ephemeral Cloudflare tunnels whose public HTTPS URL is
        only known after the child process starts. Persisted configuration is
        intentionally left unchanged because the URL changes on every start.
        """
        base = public_url.rstrip("/")
        resource = f"{base}/mcp"
        self.public_url = base
        self.resource = resource
        self.signer.set_issuer(base, resource)

    def set_chatgpt_tunnel_id(self, tunnel_id: str) -> None:
        """Update the exact Secure Tunnel audience accepted by this process."""
        self.chatgpt_tunnel_id = tunnel_id.strip()

    def accepts_resource(self, resource: str) -> bool:
        """Accept the direct MCP resource or this instance's rewritten tunnel URL.

        Secure MCP Tunnel rewrites PRMD ``resource`` to its public
        ``/v1/mcp/{tunnel_id}`` endpoint. Keep that dynamic audience bounded to
        the configured tunnel id and OpenAI-owned HTTPS hosts.
        """
        if resource == self.resource:
            return True
        tunnel_id = self.chatgpt_tunnel_id
        if not tunnel_id or not re.fullmatch(r"tunnel_[0-9a-f]{32}", tunnel_id):
            return False
        try:
            parsed = urlparse(resource)
        except ValueError:
            return False
        hostname = (parsed.hostname or "").lower()
        openai_host = any(
            hostname == suffix or hostname.endswith("." + suffix)
            for suffix in ("openai.org", "openai.com", "chatgpt.com")
        )
        return (
            parsed.scheme == "https" and openai_host
            and parsed.path == f"/v1/mcp/{tunnel_id}"
            and not parsed.params and not parsed.query and not parsed.fragment
            and parsed.username is None and parsed.password is None
        )

    def authenticate(self, authorization_header: Optional[str], remote_addr: str = "") -> Optional[Principal]:
        if self.mode == "noauth":
            if remote_addr in ("127.0.0.1", "::1", ""):
                return Principal(user_id="local", scopes=["codex"])
            return None
        if not authorization_header or not authorization_header.lower().startswith("bearer "):
            return None
        token = authorization_header[7:].strip()
        if self.mode in ("token", "both"):
            if (self.settings.mcp_access_token and
                    hmac.compare_digest(token, self.settings.mcp_access_token)):
                return Principal(user_id="mcp-token", scopes=["codex"],
                                 client_id="mcp-token")
            if self.mode == "token":
                return None
        if self.mode in ("oauth", "both"):
            return self.signer.verify(token, self.accepts_resource)
        return None


class WebAuthenticator:
    """Authenticate the admin SPA and REST API with the Web Access Token."""

    def __init__(self, token: str):
        self.token = token

    def authenticate(self, token: Optional[str]) -> Optional[Principal]:
        if token and self.token and hmac.compare_digest(token, self.token):
            return Principal(user_id="web-admin", scopes=["admin"])
        return None


def verify_pkce_challenge(verifier: str, challenge: str) -> bool:
    return hmac.compare_digest(_b64url(hashlib.sha256(verifier.encode()).digest()), challenge)


def _valid_redirect_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
        if (parsed.fragment or parsed.username or parsed.password
                or not parsed.hostname):
            return False
        if parsed.scheme == "https" and parsed.netloc:
            return True
        return (parsed.scheme == "http" and
                (parsed.hostname in ("127.0.0.1", "localhost", "::1")))
    except Exception:
        return False


def is_chatgpt_connector_callback(uri: str) -> bool:
    """Return whether a redirect URI is a production ChatGPT connector callback."""
    try:
        parsed = urlparse(uri)
        prefix = "/connector/oauth/"
        suffix = parsed.path[len(prefix):] if parsed.path.startswith(prefix) else ""
        return (
            parsed.scheme == "https"
            and parsed.hostname == "chatgpt.com"
            and parsed.port is None
            and bool(suffix)
            and "/" not in suffix
            and "%2f" not in suffix.lower()
            and "%5c" not in suffix.lower()
            and not parsed.fragment
            and not parsed.username
            and not parsed.password
        )
    except Exception:
        return False
