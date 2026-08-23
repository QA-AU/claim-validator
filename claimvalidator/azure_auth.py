"""Azure AD (Entra ID) JWT-based auth — opt-in, sitting alongside
phases/api_auth.py's single shared secret rather than replacing it
outright. Enabled by setting CLAIMVAL_AZURE_TENANT_ID; every request under
/api/* then needs a valid Azure AD access token carrying the configured
app role, instead of the shared CLAIMVAL_API_TOKEN. Unset, nothing here
changes behaviour at all — the shared-secret check stays active, which is
what lets local development and the test suite keep working without a
real Azure AD tenant.

### Why one role is enough

Each tenant already gets its own app registration under the silo model
already built — its own client_id, its own role assignment, its own
container and database. There is no finer-grained distinction to make
*within* one tenant's own deployment: a caller either holds a valid token
for this tenant's app, or doesn't. So validation here checks for exactly
one configured role rather than modelling a role hierarchy this project
has no actual use for yet — see CLAIMVAL_AZURE_REQUIRED_ROLE below.

### What is and isn't verified here

Signature, issuer, audience, expiry, and the required role claim — the
things that make a token trustworthy at all. Not verified: anything about
*who* the token was issued to beyond that (no per-user identity is tracked
downstream), since the silo model already puts that boundary at the
deployment level, not inside application code.
"""

import logging
import os
from typing import Any, Dict, Optional

import jwt

logger = logging.getLogger(__name__)

TENANT_ID_ENV = "CLAIMVAL_AZURE_TENANT_ID"
CLIENT_ID_ENV = "CLAIMVAL_AZURE_CLIENT_ID"  # audience — this API's own app registration
REQUIRED_ROLE_ENV = "CLAIMVAL_AZURE_REQUIRED_ROLE"
DEFAULT_ROLE = "Validation.User"

_jwks_client: Optional[Any] = None


def enabled() -> bool:
    """Whether Azure AD auth is configured at all. False means
    phases/api_auth.py's shared secret is what actually gates requests —
    checked by the caller in api.py, not decided in here."""
    return bool(os.getenv(TENANT_ID_ENV, "").strip())


def _config():
    tenant_id = os.getenv(TENANT_ID_ENV, "").strip()
    client_id = os.getenv(CLIENT_ID_ENV, "").strip()
    role = os.getenv(REQUIRED_ROLE_ENV, "").strip() or DEFAULT_ROLE
    return tenant_id, client_id, role


def _jwks_client_for(tenant_id: str):
    """Cached across calls — PyJWKClient does its own internal caching of
    the fetched keys too, but this avoids even re-constructing the client
    (and re-resolving the discovery URL) on every single request."""
    global _jwks_client
    if _jwks_client is None:
        jwks_url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        _jwks_client = jwt.PyJWKClient(jwks_url)
    return _jwks_client


def reset_jwks_client(client: Optional[Any] = None) -> None:
    """For tests: inject a fake client (anything with a
    get_signing_key_from_jwt(token) -> object-with-.key method), or pass
    None to force a fresh real PyJWKClient on the next call."""
    global _jwks_client
    _jwks_client = client


def validate(token: str) -> Optional[Dict[str, Any]]:
    """The decoded claims if `token` is a valid Azure AD access token for
    this tenant carrying the required app role — None for every kind of
    failure (bad signature, wrong tenant, wrong audience, expired, missing
    role). Collapsed to one answer because that's all a caller needs to
    act on; the specific reason is logged, not silently dropped.
    """
    tenant_id, client_id, role = _config()
    if not tenant_id or not token:
        return None

    try:
        signing_key = _jwks_client_for(tenant_id).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=client_id or None,
            issuer=f"https://login.microsoftonline.com/{tenant_id}/v2.0",
            options={"require": ["exp", "iss"], "verify_aud": bool(client_id)},
        )
    except jwt.PyJWTError as e:
        logger.warning(f"[AzureAD] token rejected: {e}")
        return None

    roles = claims.get("roles") or []
    if role not in roles:
        logger.warning(
            f"[AzureAD] token valid but missing required role {role!r} (has {roles})"
        )
        return None

    return claims
