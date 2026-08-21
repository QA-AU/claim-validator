"""Authentication for the claim-validator API — copied from the source repo's
phases/api_auth.py verbatim except the env var and cookie names below.

The API had none. Every endpoint — reading ontologies, spending money on model
calls, writing files — was open to anything that could reach the port, and CORS
was configured `allow_origins=["*"]` with `allow_credentials=True`, which
Starlette implements by **echoing whatever origin asked**. Measured against a
running server:

    Origin: https://evil.example
    -> access-control-allow-origin: https://evil.example
       access-control-allow-credentials: true

So the exposure was not merely "localhost is trusted". Any website the user
visited could drive this API from their browser, with credentials, including the
endpoints that spend money.

### Fail closed, by path

The gate is **middleware over `/api/*`**, not a dependency on each route. There
are forty-odd routes and more are added regularly; a per-route dependency is one
that someone forgets, and a forgotten one is an open endpoint that looks
protected. Anything under `/api/` is denied unless it is explicitly listed as
public, so a new route is protected by default and being wrong costs a 401
rather than a silent hole.

### What is deliberately not gated

Static assets and the HTML shells. They contain no data — everything the app
knows arrives over `/api/*`. Gating them would mean an unauthenticated user gets
a broken page instead of a login prompt, with no security gained.

`/api/ping` is public and answers only "the server is running". A health check
that reports configuration is not a health check.

### Sessions

A browser cannot hold the token in JavaScript without becoming the place the
token leaks from — which is the mistake this codebase just fixed for API keys.
So `POST /api/session` exchanges the token for an **HttpOnly, SameSite=Strict**
cookie: unreadable by script, and not sent on cross-site requests, which is a
second independent defence against the drive-by case above.

The cookie carries the token itself rather than a session id. That is a
deliberate simplification for a single-user local workbench: there is no session
store to expire, revoke or persist, and a restart with a fresh token invalidates
everything automatically.
"""

import logging
import os
import secrets
from typing import Optional, Set

logger = logging.getLogger(__name__)

TOKEN_ENV = "CLAIMVAL_API_TOKEN"
COOKIE_NAME = "claimval_session"
HEADER_NAME = "X-API-Token"

# Paths under /api that answer without credentials. Everything else is denied.
PUBLIC_PATHS: Set[str] = {
    "/api/ping",      # liveness only — says nothing about configuration
    "/api/session",   # the exchange that grants access cannot itself require it
}

_token: Optional[str] = None


def configured_token() -> str:
    """The shared secret, from the environment or generated for this run.

    A generated token is printed once at startup. That is what makes the default
    usable: an operator who sets nothing still gets a protected server rather
    than an open one, which is the opposite of the previous default.
    """
    global _token
    if _token is None:
        supplied = (os.getenv(TOKEN_ENV) or "").strip()
        _token = supplied or secrets.token_urlsafe(32)
        if not supplied:
            logger.warning(
                "No %s set; generated a token for this run:\n\n    %s\n\n"
                "Set %s to keep it stable across restarts.",
                TOKEN_ENV, _token, TOKEN_ENV,
            )
    return _token


def reset_token(value: Optional[str] = None) -> str:
    """Set the token explicitly. For tests and for rotation."""
    global _token
    _token = value
    return configured_token()


def token_matches(candidate: Optional[str]) -> bool:
    """Constant-time comparison, so the token cannot be guessed byte by byte."""
    if not candidate:
        return False
    return secrets.compare_digest(str(candidate), configured_token())


def is_public(path: str) -> bool:
    """Whether a path answers without credentials.

    Only `/api/*` is gated at all, so anything outside it is public by
    definition — the static shell is code, not data.
    """
    if not path.startswith("/api/") and path != "/api":
        return True
    return path.rstrip("/") in PUBLIC_PATHS or path in PUBLIC_PATHS


def presented_token(headers, cookies) -> Optional[str]:
    """The token a request offers, from any of the three accepted places."""
    authorization = headers.get("authorization") or ""
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()

    header = headers.get(HEADER_NAME.lower()) or headers.get(HEADER_NAME)
    if header:
        return header.strip()

    return cookies.get(COOKIE_NAME)


def authorised(request) -> bool:
    """Whether this request may proceed."""
    if is_public(request.url.path):
        return True
    return token_matches(presented_token(request.headers, request.cookies))
