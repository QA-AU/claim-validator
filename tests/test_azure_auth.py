"""claimvalidator/azure_auth.py's token validation — no real Azure AD
tenant available to test against, so this generates a real RSA keypair
and signs real JWTs locally, then injects a fake JWKS client that hands
back the matching public key. Everything downstream (jwt.decode, the
issuer/audience/role checks) is the exact real code path a genuine Azure
AD token would go through — only the key-fetching step is faked.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from claimvalidator import azure_auth

TENANT_ID = "11111111-1111-1111-1111-111111111111"
CLIENT_ID = "22222222-2222-2222-2222-222222222222"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_key = _private_key.public_key()


class _FakeSigningKey:
    def __init__(self, key):
        self.key = key


class _FakeJWKClient:
    """Stands in for jwt.PyJWKClient — same one method actually used,
    get_signing_key_from_jwt, always resolving to the local test key
    regardless of the token's kid, since there's no real JWKS endpoint
    to match against here."""

    def get_signing_key_from_jwt(self, token):
        return _FakeSigningKey(_public_key)


def _token(claims_override=None, key=None):
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": int(time.time()) + 300,
        "roles": ["Validation.User"],
    }
    claims.update(claims_override or {})
    return jwt.encode(claims, key or _private_key, algorithm="RS256")


@pytest.fixture(autouse=True)
def _configure(monkeypatch):
    monkeypatch.setenv(azure_auth.TENANT_ID_ENV, TENANT_ID)
    monkeypatch.setenv(azure_auth.CLIENT_ID_ENV, CLIENT_ID)
    monkeypatch.delenv(azure_auth.REQUIRED_ROLE_ENV, raising=False)
    azure_auth.reset_jwks_client(_FakeJWKClient())
    yield
    azure_auth.reset_jwks_client(None)


def test_disabled_when_no_tenant_id_is_configured(monkeypatch):
    monkeypatch.delenv(azure_auth.TENANT_ID_ENV, raising=False)
    assert azure_auth.enabled() is False
    assert azure_auth.validate(_token()) is None


def test_enabled_when_tenant_id_is_set():
    assert azure_auth.enabled() is True


def test_a_valid_token_with_the_required_role_is_accepted():
    claims = azure_auth.validate(_token())
    assert claims is not None
    assert claims["roles"] == ["Validation.User"]


def test_a_token_missing_the_required_role_is_rejected():
    assert azure_auth.validate(_token({"roles": ["SomeOtherRole"]})) is None


def test_a_token_with_no_roles_claim_at_all_is_rejected():
    assert azure_auth.validate(_token({"roles": []})) is None


def test_wrong_audience_is_rejected():
    assert azure_auth.validate(_token({"aud": "not-this-api"})) is None


def test_wrong_issuer_is_rejected():
    assert azure_auth.validate(_token({"iss": "https://login.microsoftonline.com/wrong-tenant/v2.0"})) is None


def test_an_expired_token_is_rejected():
    assert azure_auth.validate(_token({"exp": int(time.time()) - 60})) is None


def test_a_token_signed_with_the_wrong_key_is_rejected():
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert azure_auth.validate(_token(key=other_key)) is None


def test_an_empty_token_is_rejected_not_crashed_on():
    assert azure_auth.validate("") is None


def test_a_custom_required_role_is_honoured(monkeypatch):
    monkeypatch.setenv(azure_auth.REQUIRED_ROLE_ENV, "Custom.Role")
    assert azure_auth.validate(_token({"roles": ["Validation.User"]})) is None
    assert azure_auth.validate(_token({"roles": ["Custom.Role"]})) is not None
