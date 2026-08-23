"""db/database.py's Azure AD Postgres auth path — no real Azure AD tenant
or Postgres server available to test end to end, so this verifies the two
things that are actually this project's own code: the env-var switch, and
the token-fetch-and-inject logic, with a fake credential standing in for
DefaultAzureCredential. Wiring that logic onto a real engine's do_connect
event is verified against a real SQLAlchemy engine (SQLite, standing in
for the transport — the do_connect event fires the same way regardless of
dialect), so only the actual network call to Azure AD is unverified here.
"""

from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, event

from db.database import (
    AZURE_POSTGRES_SCOPE,
    _apply_azure_ad_token,
    _install_azure_ad_token_provider,
    _use_azure_ad_auth,
)


class _StopHere(Exception):
    """Raised deliberately once cparams has been captured, so SQLite's
    real driver — which has no `password` parameter and would reject a
    stub connection object during SQLAlchemy's own post-connect
    introspection — never actually gets reached. What's under test is
    whether the token provider ran and set the password, not whether
    SQLite accepts one."""


def _short_circuit_the_real_connect(engine, seen_cparams):
    """A second do_connect listener, registered after
    _install_azure_ad_token_provider's — captures cparams (already
    mutated by the token provider at this point, since SQLAlchemy calls
    listeners in registration order) then aborts before any real driver
    call happens.
    """
    @event.listens_for(engine, "do_connect")
    def _stub(dialect, conn_rec, cargs, cparams):
        seen_cparams.append(dict(cparams))
        raise _StopHere()


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CLAIMVAL_DB_AAD_AUTH", raising=False)
    assert _use_azure_ad_auth() is False


def test_enabled_by_true_ish_values(monkeypatch):
    for value in ("1", "true", "True", "yes", "YES"):
        monkeypatch.setenv("CLAIMVAL_DB_AAD_AUTH", value)
        assert _use_azure_ad_auth() is True


def test_disabled_by_anything_else(monkeypatch):
    for value in ("0", "false", "", "no"):
        monkeypatch.setenv("CLAIMVAL_DB_AAD_AUTH", value)
        assert _use_azure_ad_auth() is False


def test_apply_azure_ad_token_sets_the_password_from_a_fresh_token():
    fake_token = MagicMock()
    fake_token.token = "a-fresh-access-token"
    fake_credential = MagicMock()
    fake_credential.get_token.return_value = fake_token

    cparams = {}
    _apply_azure_ad_token(fake_credential, cparams)

    assert cparams["password"] == "a-fresh-access-token"
    fake_credential.get_token.assert_called_once_with(AZURE_POSTGRES_SCOPE)


def test_apply_azure_ad_token_fetches_again_each_call_not_cached():
    """The whole point: a pooled connection reconnecting an hour later
    must not replay the same (by then expired) token."""
    fake_credential = MagicMock()
    fake_credential.get_token.side_effect = [
        MagicMock(token="token-1"),
        MagicMock(token="token-2"),
    ]

    cparams_a, cparams_b = {}, {}
    _apply_azure_ad_token(fake_credential, cparams_a)
    _apply_azure_ad_token(fake_credential, cparams_b)

    assert cparams_a["password"] == "token-1"
    assert cparams_b["password"] == "token-2"


def test_install_azure_ad_token_provider_wires_a_real_do_connect_listener():
    """Real SQLAlchemy engine, real event system — only DefaultAzureCredential
    itself is faked, since that's the one call that would otherwise reach
    a real Azure AD endpoint."""
    engine = create_engine("sqlite:///:memory:")
    fake_token = MagicMock()
    fake_token.token = "wired-token"
    fake_credential_instance = MagicMock()
    fake_credential_instance.get_token.return_value = fake_token
    seen_cparams = []

    with patch("azure.identity.DefaultAzureCredential", return_value=fake_credential_instance):
        _install_azure_ad_token_provider(engine)
        _short_circuit_the_real_connect(engine, seen_cparams)
        try:
            engine.connect()
        except _StopHere:
            pass

    fake_credential_instance.get_token.assert_called_with(AZURE_POSTGRES_SCOPE)
    assert seen_cparams[0]["password"] == "wired-token"


def test_installing_the_provider_fetches_a_new_token_on_every_new_connection():
    engine = create_engine("sqlite:///:memory:")
    fake_credential_instance = MagicMock()
    fake_credential_instance.get_token.side_effect = [
        MagicMock(token="t1"), MagicMock(token="t2"), MagicMock(token="t3"),
    ]
    seen_cparams = []

    with patch("azure.identity.DefaultAzureCredential", return_value=fake_credential_instance):
        _install_azure_ad_token_provider(engine)
        _short_circuit_the_real_connect(engine, seen_cparams)
        for _ in range(3):
            try:
                engine.connect()
            except _StopHere:
                pass

    assert fake_credential_instance.get_token.call_count == 3
    assert [c["password"] for c in seen_cparams] == ["t1", "t2", "t3"]
