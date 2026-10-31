"""Entry point: Garmin Personal API MCP server over HTTP, behind single-user OAuth.

Wraps the upstream `garmin-personal-mcp` server (read-only) and adds:
- OAuth 2.1 with dynamic client registration, so claude.ai can connect as a
  custom connector; access requires LOGIN_PASSWORD.
- garmin_get_activity_streams: per-interval HR/speed/cadence/power from the
  activity's .FIT file, instead of saving that file on the server's disk.

Required environment (set as Fly secrets):
  GARMIN_PERSONAL_API_KEY  Garmin Connect Personal API key
  LOGIN_PASSWORD           password you type when connecting a client (>= 12 chars)
  OAUTH_SIGNING_KEY        random secret, e.g. `openssl rand -hex 32`
Optional:
  PUBLIC_URL               default https://$FLY_APP_NAME.fly.dev
  ALLOWED_REDIRECT_HOSTS   default claude.ai,claude.com
  PORT                     default 8080
"""

from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.types import ToolAnnotations

from garmin_personal_mcp import garmin_client
from garmin_personal_mcp.cli import transport_security
from garmin_personal_mcp.server import mcp

from oauth import SingleUserOAuthProvider
from streams import parse_fit


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing required environment variable {name}")
    return value


require("GARMIN_PERSONAL_API_KEY")
login_password = require("LOGIN_PASSWORD")
signing_key = require("OAUTH_SIGNING_KEY")

app_name = os.environ.get("FLY_APP_NAME")
public_url = os.environ.get("PUBLIC_URL") or (f"https://{app_name}.fly.dev" if app_name else "")
if not public_url:
    sys.exit("Set PUBLIC_URL (or run on Fly, which sets FLY_APP_NAME)")
public_url = public_url.rstrip("/")
public_host = urlparse(public_url).hostname
redirect_hosts = [
    h.strip()
    for h in os.environ.get("ALLOWED_REDIRECT_HOSTS", "claude.ai,claude.com").split(",")
    if h.strip()
]

provider = SingleUserOAuthProvider(signing_key, login_password, public_url, redirect_hosts)

# Configure auth on the upstream FastMCP instance before the HTTP app is built.
mcp.settings.auth = AuthSettings(
    issuer_url=public_url,
    resource_server_url=f"{public_url}/mcp",
    client_registration_options=ClientRegistrationOptions(enabled=True),
    revocation_options=RevocationOptions(enabled=False),
    validate_token_resource=False,
)
mcp._auth_server_provider = provider
mcp._token_verifier = ProviderTokenVerifier(provider)
mcp.settings.host = "0.0.0.0"
mcp.settings.port = int(os.environ.get("PORT", "8080"))
mcp.settings.transport_security = transport_security([public_host])

# Saving a FIT file on the server's disk is useless to a remote client.
mcp.remove_tool("garmin_download_activity")


@mcp.tool(
    title="Get activity streams",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def garmin_get_activity_streams(
    activity_id: int, interval_s: int = 10, include_gps: bool = False
) -> dict:
    """Time series and laps from the activity's original .FIT file: one row
    per `interval_s` seconds with t (s since start), hr, speed_ms, distance_m,
    cadence, power_w, altitude_m, temp_c (and lat/lon if include_gps). Use for
    pace/HR curves, zone time and decoupling. interval_s 1-60, default 10.

    activity_id: activityId from garmin_search_activities or garmin_get_day.
    """
    interval_s = min(max(interval_s, 1), 60)
    content = await asyncio.to_thread(
        garmin_client.call, lambda c: c.download_activity(activity_id)
    )
    return await asyncio.to_thread(parse_fit, content, interval_s, include_gps)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


@mcp.custom_route("/login", methods=["GET", "POST"])
async def login(request: Request):
    return await provider.login(request)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
