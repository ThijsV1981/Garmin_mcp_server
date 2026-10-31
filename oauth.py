"""Stateless single-user OAuth 2.1 provider for a personal MCP server.

Everything the server would normally keep in a database (registered clients,
authorization codes, access and refresh tokens) is instead an HMAC-signed blob,
so it survives the Fly machine stopping and starting. One person, one login
password. Rotating OAUTH_SIGNING_KEY invalidates every client and token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import secrets
import time
from urllib.parse import urlencode, urlparse

from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

CODE_TTL = 5 * 60
ACCESS_TTL = 60 * 60
REFRESH_TTL = 90 * 24 * 60 * 60
LOGIN_TTL = 10 * 60
SUBJECT = "owner"


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    def __init__(self, key: str):
        if len(key) < 32:
            raise ValueError("OAUTH_SIGNING_KEY must be at least 32 characters")
        self._key = key.encode()

    def _mac(self, purpose: str, body: str) -> str:
        msg = f"{purpose}.{body}".encode()
        return _b64e(hmac.new(self._key, msg, hashlib.sha256).digest())

    def sign(self, purpose: str, payload: dict) -> str:
        body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
        return f"{body}.{self._mac(purpose, body)}"

    def verify(self, purpose: str, token: str) -> dict | None:
        try:
            body, mac = token.rsplit(".", 1)
        except ValueError:
            return None
        if not hmac.compare_digest(mac, self._mac(purpose, body)):
            return None
        try:
            payload = json.loads(_b64d(body))
        except ValueError:
            return None
        exp = payload.get("exp")
        if exp is not None and exp < time.time():
            return None
        return payload

    def derive(self, purpose: str, value: str) -> str:
        return self._mac(purpose, value)


class SingleUserOAuthProvider:
    def __init__(
        self,
        signing_key: str,
        login_password: str,
        public_url: str,
        allowed_redirect_hosts: list[str],
    ):
        if len(login_password) < 12:
            raise ValueError("LOGIN_PASSWORD must be at least 12 characters")
        self.signer = Signer(signing_key)
        self.password = login_password
        self.public_url = public_url.rstrip("/")
        self.allowed_redirect_hosts = [h.lower() for h in allowed_redirect_hosts]
        self._login_lock = asyncio.Lock()

    # --- clients -----------------------------------------------------------

    def _redirect_allowed(self, uri: str) -> bool:
        parsed = urlparse(uri)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https":
            return False
        return any(host == h or host.endswith("." + h) for h in self.allowed_redirect_hosts)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris or not all(self._redirect_allowed(u) for u in uris):
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="redirect_uri host is not allowed on this server",
            )
        meta = client_info.model_dump(
            mode="json",
            exclude={"client_id", "client_secret", "client_secret_expires_at"},
            exclude_none=True,
        )
        client_id = self.signer.sign("client", meta)
        client_info.client_id = client_id
        if client_info.client_secret is not None:
            client_info.client_secret = self.signer.derive("client-secret", client_id)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        meta = self.signer.verify("client", client_id)
        if meta is None:
            return None
        info = OAuthClientInformationFull(**meta, client_id=client_id)
        if info.token_endpoint_auth_method not in (None, "none"):
            info.client_secret = self.signer.derive("client-secret", client_id)
        return info

    # --- authorization -----------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        request = self.signer.sign(
            "login",
            {
                "client_id": client.client_id,
                "client_name": client.client_name,
                "state": params.state,
                "scopes": params.scopes or [],
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "explicit": params.redirect_uri_provided_explicitly,
                "resource": params.resource,
                "exp": int(time.time()) + LOGIN_TTL,
            },
        )
        return f"{self.public_url}/login?{urlencode({'request': request})}"

    def _login_page(self, request_token: str, client_name: str, redirect_host: str, error: str = "") -> HTMLResponse:
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        body = f"""<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Garmin MCP</title>
<style>body{{background:#070B16;color:#E5E7EB;font:16px system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;padding:16px}}
form{{max-width:360px;width:100%}}h1{{font-size:20px}}p{{color:#9CA3AF}}.err{{color:#F43F5E}}
input,button{{width:100%;box-sizing:border-box;padding:12px;margin-top:12px;border-radius:8px;font-size:16px}}
input{{background:#111827;border:1px solid #374151;color:#E5E7EB}}button{{background:#FBBF24;border:0;font-weight:600;cursor:pointer}}</style>
</head><body><form method="post" action="/login">
<h1>Garmin MCP</h1>
<p>{html.escape(client_name or "Een app")} vraagt toegang tot je Garmin-data en stuurt je daarna terug naar <b>{html.escape(redirect_host)}</b>.</p>
{err}<input type="hidden" name="request" value="{html.escape(request_token)}">
<input type="password" name="password" placeholder="Wachtwoord" autocomplete="current-password" autofocus required>
<button type="submit">Toegang geven</button></form></body></html>"""
        return HTMLResponse(body, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    async def login(self, request: Request) -> Response:
        if request.method == "GET":
            token = request.query_params.get("request", "")
            data = self.signer.verify("login", token)
            if data is None:
                return HTMLResponse("Verlopen of ongeldig verzoek. Start de koppeling opnieuw.", status_code=400)
            return self._login_page(token, data.get("client_name") or "", urlparse(data["redirect_uri"]).hostname or "")

        form = await request.form()
        token = str(form.get("request", ""))
        data = self.signer.verify("login", token)
        if data is None:
            return HTMLResponse("Verlopen of ongeldig verzoek. Start de koppeling opnieuw.", status_code=400)

        async with self._login_lock:
            ok = hmac.compare_digest(str(form.get("password", "")).encode(), self.password.encode())
            if not ok:
                await asyncio.sleep(2)  # slow down guessing
        if not ok:
            return self._login_page(
                token, data.get("client_name") or "", urlparse(data["redirect_uri"]).hostname or "", "Onjuist wachtwoord."
            )

        code = self.signer.sign(
            "code",
            {
                "client_id": data["client_id"],
                "scopes": data["scopes"],
                "code_challenge": data["code_challenge"],
                "redirect_uri": data["redirect_uri"],
                "explicit": data["explicit"],
                "resource": data["resource"],
                "nonce": secrets.token_urlsafe(8),
                "exp": int(time.time()) + CODE_TTL,
            },
        )
        return RedirectResponse(
            construct_redirect_uri(data["redirect_uri"], code=code, state=data["state"]),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = self.signer.verify("code", authorization_code)
        if data is None or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=data["exp"],
            client_id=data["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=AnyUrl(data["redirect_uri"]),
            redirect_uri_provided_explicitly=data["explicit"],
            resource=data["resource"],
            subject=SUBJECT,
        )

    # --- tokens ------------------------------------------------------------

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        now = int(time.time())
        access = self.signer.sign(
            "access",
            {"client_id": client_id, "scopes": scopes, "resource": resource, "exp": now + ACCESS_TTL},
        )
        refresh = self.signer.sign(
            "refresh",
            {
                "client_id": client_id,
                "scopes": scopes,
                "resource": resource,
                "nonce": secrets.token_urlsafe(8),
                "exp": now + REFRESH_TTL,
            },
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self.signer.verify("refresh", refresh_token)
        if data is None or data["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=data["exp"],
            resource=data["resource"],
            subject=SUBJECT,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        requested = scopes or refresh_token.scopes
        if not set(requested) <= set(refresh_token.scopes):
            raise TokenError(error="invalid_scope", error_description="scope exceeds original grant")
        return self._issue(client.client_id, requested, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self.signer.verify("access", token)
        if data is None:
            return None
        return AccessToken(
            token=token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=data["exp"],
            resource=data["resource"],
            subject=SUBJECT,
        )

    async def revoke_token(self, token) -> None:
        # Stateless: tokens cannot be revoked individually.
        # Rotate OAUTH_SIGNING_KEY to revoke everything.
        return None
