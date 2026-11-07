"""Streamable-HTTP transport with a shared-secret guard, for remote use (claude.ai, mobile)."""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any

log = logging.getLogger(__name__)


class TokenGuard:
    """ASGI middleware: require `Authorization: Bearer <token>` or `?token=<token>`.

    claude.ai custom connectors can't send arbitrary headers, so the token can also be
    embedded in the connector URL (https://host/mcp?token=...). Use a long random value.
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") == "/healthz":
            await self.app(scope, receive, send)
            return
        supplied = b""
        for k, v in scope.get("headers", []):
            if k == b"authorization" and v.lower().startswith(b"bearer "):
                supplied = v[7:].strip()
                break
        if not supplied:
            from urllib.parse import parse_qs

            qs = parse_qs(scope.get("query_string", b"").decode())
            supplied = (qs.get("token") or [""])[0].encode()
        if not supplied or not hmac.compare_digest(supplied, self.token):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        await self.app(scope, receive, send)


def build_app(server: Any, host: str) -> Any:
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    allowed = [h.strip() for h in os.getenv("GARMIN_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = None
    if allowed:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed,
            allowed_origins=[f"https://{h}" for h in allowed] + ["https://claude.ai"],
        )
    elif host not in ("127.0.0.1", "localhost", "::1"):
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    app = server.streamable_http_app(host=host, transport_security=security, stateless_http=True,
                                     json_response=True)
    app.router.routes.append(Route("/healthz", lambda request: PlainTextResponse("ok")))

    token = os.getenv("GARMIN_MCP_TOKEN")
    if token:
        return TokenGuard(app, token)
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(
            "Refusing to serve your Garmin data on a public interface without GARMIN_MCP_TOKEN. "
            "Set it to a long random string (e.g. `python -c 'import secrets;print(secrets.token_urlsafe(32))'`)."
        )
    return app


def serve(server: Any, host: str, port: int) -> None:
    import uvicorn

    app = build_app(server, host)
    log.info("Serving MCP over streamable HTTP at http://%s:%s/mcp", host, port)
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*")
