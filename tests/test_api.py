"""API-layer smoke tests: the auth gate and the one always-public route.

Does not exercise the actual validation pipeline (that's covered by
scripts/validate_claims.py against a real model, and by pipeline.py's own
building blocks in the other test files) — this only proves the HTTP layer
wires request/response correctly and that the auth middleware fails closed.
"""

import os
import tempfile

# api.py opens the DB at import time, so the env var must be set first —
# a fixture running after import would be too late. Same reasoning for
# CLAIMVAL_SOURCE_DIR: config.py reads it at import time too.
_tmp_db = tempfile.mkdtemp()
_tmp_source = tempfile.mkdtemp()
os.environ["CLAIMVAL_DB_URL"] = f"sqlite:///{_tmp_db}/test_api.db"
os.environ["CLAIMVAL_API_TOKEN"] = "test-suite-token"
os.environ["CLAIMVAL_SOURCE_DIR"] = _tmp_source

from fastapi.testclient import TestClient  # noqa: E402

from claimvalidator import config  # noqa: E402
from claimvalidator.api import app  # noqa: E402
from claimvalidator.jobs import create_job, mark_done, mark_running  # noqa: E402
from phases.api_auth import reset_token  # noqa: E402

reset_token("test-suite-token")

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-suite-token"}


def test_ping_is_public():
    response = client.get("/api/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_protected_route_401s_without_a_token():
    response = client.get("/api/ontologies/does-not-exist")
    assert response.status_code == 401


def test_protected_route_works_with_the_correct_token():
    response = client.get(
        "/api/ontologies/does-not-exist",
        headers={"Authorization": "Bearer test-suite-token"},
    )
    # 404, not 401 — the token was accepted, the route just found nothing.
    assert response.status_code == 404


def test_wrong_token_still_401s():
    response = client.get(
        "/api/ontologies/does-not-exist",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------- azure ad

def test_when_azure_ad_is_enabled_the_shared_secret_no_longer_works(monkeypatch):
    """The whole point of azure_auth being opt-in: once configured, it's
    the only thing that decides, not a second acceptable path alongside
    the old shared secret."""
    import time
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from claimvalidator import azure_auth

    tenant_id = "33333333-3333-3333-3333-333333333333"
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class _FakeSigningKey:
        key = private_key.public_key()

    class _FakeJWKClient:
        def get_signing_key_from_jwt(self, token):
            return _FakeSigningKey()

    monkeypatch.setenv(azure_auth.TENANT_ID_ENV, tenant_id)
    monkeypatch.delenv(azure_auth.CLIENT_ID_ENV, raising=False)
    azure_auth.reset_jwks_client(_FakeJWKClient())
    try:
        # The old shared secret, still technically valid for api_auth, no
        # longer works once azure_auth is enabled.
        response = client.get("/api/ontologies/does-not-exist", headers=AUTH)
        assert response.status_code == 401

        good_token = pyjwt.encode(
            {"iss": f"https://login.microsoftonline.com/{tenant_id}/v2.0",
             "exp": int(time.time()) + 300, "roles": ["Validation.User"]},
            private_key, algorithm="RS256",
        )
        response = client.get(
            "/api/ontologies/does-not-exist",
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert response.status_code == 404  # accepted; route just found nothing

        bad_token = pyjwt.encode(
            {"iss": f"https://login.microsoftonline.com/{tenant_id}/v2.0",
             "exp": int(time.time()) + 300, "roles": ["WrongRole"]},
            private_key, algorithm="RS256",
        )
        response = client.get(
            "/api/ontologies/does-not-exist",
            headers={"Authorization": f"Bearer {bad_token}"},
        )
        assert response.status_code == 401
    finally:
        azure_auth.reset_jwks_client(None)


def test_a_users_job_is_invisible_to_a_different_authenticated_user(monkeypatch):
    """The concrete fix for the authorization-bug scenario this was built
    from: a valid token from a *different* user must not be able to read
    someone else's job or report, even knowing the exact job_id."""
    import time
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from claimvalidator import azure_auth

    tenant_id = "44444444-4444-4444-4444-444444444444"
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class _FakeSigningKey:
        key = private_key.public_key()

    class _FakeJWKClient:
        def get_signing_key_from_jwt(self, token):
            return _FakeSigningKey()

    def _token_for(oid: str) -> str:
        return pyjwt.encode(
            {"iss": f"https://login.microsoftonline.com/{tenant_id}/v2.0",
             "exp": int(time.time()) + 300, "roles": ["Validation.User"], "oid": oid},
            private_key, algorithm="RS256",
        )

    monkeypatch.setenv(azure_auth.TENANT_ID_ENV, tenant_id)
    monkeypatch.delenv(azure_auth.CLIENT_ID_ENV, raising=False)
    azure_auth.reset_jwks_client(_FakeJWKClient())
    try:
        user_a = {"Authorization": f"Bearer {_token_for('user-a-oid')}"}
        user_b = {"Authorization": f"Bearer {_token_for('user-b-oid')}"}

        submit = client.post(
            "/api/validations", headers=user_a,
            json={
                "document": {"document_id": "test-doc", "files": ["does/not/exist.txt"]},
                "claims": [{"id": "C1", "text": "a claim"}],
            },
        )
        assert submit.status_code == 202
        job_id = submit.json()["job_id"]

        # The owner can see their own job.
        assert client.get(f"/api/validations/{job_id}", headers=user_a).status_code == 200

        # A different authenticated user, with a perfectly valid token,
        # cannot — 404, not 403, so existence isn't confirmed either.
        assert client.get(f"/api/validations/{job_id}", headers=user_b).status_code == 404
        assert client.get(f"/api/validations/{job_id}/report", headers=user_b).status_code == 404
    finally:
        azure_auth.reset_jwks_client(None)


def test_api_ping_stays_public_regardless_of_which_auth_backend_is_active(monkeypatch):
    from claimvalidator import azure_auth

    monkeypatch.setenv(azure_auth.TENANT_ID_ENV, "some-tenant")
    response = client.get("/api/ping")
    assert response.status_code == 200


def test_submit_validation_returns_immediately_with_a_job_id():
    response = client.post(
        "/api/validations",
        headers={"Authorization": "Bearer test-suite-token"},
        json={
            "document": {"document_id": "test-doc", "files": ["does/not/exist.txt"]},
            "claims": [{"id": "C1", "text": "a claim"}],
        },
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["job_id"].startswith("job_")


def test_submit_validation_with_neither_files_nor_ontology_key_is_rejected():
    response = client.post(
        "/api/validations",
        headers={"Authorization": "Bearer test-suite-token"},
        json={
            "document": {"document_id": "test-doc", "files": []},
            "claims": [{"id": "C1", "text": "a claim"}],
        },
    )
    assert response.status_code == 400


def test_list_ontologies_includes_a_freshly_created_one(monkeypatch, tmp_path):
    from phases.ontology_store import OntologyStore

    monkeypatch.setattr(config, "STORE_ROOT", str(tmp_path))
    store = OntologyStore(root=str(tmp_path))
    meta = store.create("RFC 6749", created_by="user-a-oid")
    meta.content_hash = "abc123"
    store._write_meta(meta)

    response = client.get("/api/ontologies", headers=AUTH)
    assert response.status_code == 200
    ontologies = response.json()["ontologies"]
    assert len(ontologies) == 1
    assert ontologies[0]["key"] == meta.key
    assert ontologies[0]["name"] == "RFC 6749"
    assert ontologies[0]["created_by"] == "user-a-oid"
    assert ontologies[0]["content_hash"] == "abc123"
    assert ontologies[0]["has_index"] is False


def test_delete_ontology_route_no_longer_exists():
    """Ontologies are shared and immutable in the multi-user model — no
    authenticated caller can destroy one via the API any more."""
    response = client.delete("/api/ontologies/does-not-exist", headers=AUTH)
    assert response.status_code == 405


def test_get_unknown_validation_job_404s():
    response = client.get(
        "/api/validations/job_doesnotexist",
        headers={"Authorization": "Bearer test-suite-token"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------- documents

def test_upload_document_writes_it_under_source_dir_and_returns_an_id():
    response = client.post(
        "/api/documents", headers=AUTH,
        files={"file": ("spec.txt", b"a reference document", "text/plain")},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "spec.txt"
    assert body["size_bytes"] == len(b"a reference document")
    assert body["document_id"].startswith("doc_")

    from pathlib import Path
    written = Path(body["path"])
    assert written.exists()
    assert written.read_bytes() == b"a reference document"
    assert written.parent.name == body["document_id"]  # one subdir per document_id


def test_upload_document_honours_a_caller_supplied_document_id():
    response = client.post(
        "/api/documents", headers=AUTH,
        data={"document_id": "rfc6749"},
        files={"file": ("rfc.txt", b"...", "text/plain")},
    )
    assert response.json()["document_id"] == "rfc6749"


def test_upload_document_rejects_an_unsupported_file_type():
    response = client.post(
        "/api/documents", headers=AUTH,
        files={"file": ("archive.zip", b"PK\x03\x04", "application/zip")},
    )
    assert response.status_code == 400


def test_upload_document_accepts_a_graphql_schema():
    """The upload gate (validate_file_type) used to be stricter than
    load_document's own fallback, which already reads any unrecognised
    suffix as plain text — .graphql/.proto specs were rejected at upload
    even though they'd have loaded fine. Widened to match."""
    response = client.post(
        "/api/documents", headers=AUTH,
        files={"file": ("schema.graphql", b"type Query { hello: String }", "text/plain")},
    )
    assert response.status_code == 201


def test_upload_document_accepts_a_protobuf_schema():
    response = client.post(
        "/api/documents", headers=AUTH,
        files={"file": ("service.proto", b'syntax = "proto3";', "text/plain")},
    )
    assert response.status_code == 201


def test_upload_document_requires_auth():
    response = client.post(
        "/api/documents",
        files={"file": ("spec.txt", b"x", "text/plain")},
    )
    assert response.status_code == 401


def test_upload_document_rejects_a_file_over_the_configured_limit(monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 10)
    response = client.post(
        "/api/documents", headers=AUTH,
        files={"file": ("spec.txt", b"this is well over ten bytes", "text/plain")},
    )
    assert response.status_code == 413


def test_upload_document_strips_path_traversal_from_the_filename():
    """A malicious filename ("../../evil.txt") must never land outside
    the document_id directory it was uploaded into — found in a
    vulnerability scan: file.filename reached the filesystem unsanitized."""
    response = client.post(
        "/api/documents", headers=AUTH,
        data={"document_id": "traversal-test"},
        files={"file": ("../../../tmp/evil.txt", b"payload", "text/plain")},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "evil.txt"

    from pathlib import Path
    written = Path(body["path"])
    assert written.parent.name == "traversal-test"
    assert written.parent == Path(config.SOURCE_DIR) / "traversal-test"


def test_upload_document_rejects_a_document_id_with_path_separators():
    response = client.post(
        "/api/documents", headers=AUTH,
        data={"document_id": "../../etc"},
        files={"file": ("spec.txt", b"x", "text/plain")},
    )
    assert response.status_code == 400


def test_a_rejected_oversized_upload_leaves_no_empty_directory_behind(monkeypatch):
    """Found live testing this endpoint end to end: the partial file was
    correctly cleaned up on a 413, but the directory it was created in was
    not — real, if minor, litter on every rejected upload."""
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 10)
    response = client.post(
        "/api/documents", headers=AUTH,
        data={"document_id": "rejected-upload-test"},
        files={"file": ("spec.txt", b"this is well over ten bytes", "text/plain")},
    )
    assert response.status_code == 413

    from pathlib import Path
    assert not (Path(config.SOURCE_DIR) / "rejected-upload-test").exists()


# ---------------------------------------------------------------- reports

def _session():
    from claimvalidator.api import _SessionLocal
    from db.database import get_session
    return get_session(_SessionLocal)


def test_report_download_404s_for_an_unknown_job():
    response = client.get("/api/validations/job_doesnotexist/report", headers=AUTH)
    assert response.status_code == 404


def test_report_download_409s_while_the_job_is_still_running():
    session = _session()
    job = create_job(session, "validation", {}, owner_user_id="local")
    mark_running(session, job.job_id)
    session.close()

    response = client.get(f"/api/validations/{job.job_id}/report", headers=AUTH)
    assert response.status_code == 409


def test_report_download_streams_the_real_file(tmp_path):
    fake_report = tmp_path / "report.xlsx"
    fake_report.write_bytes(b"fake xlsx bytes")

    session = _session()
    job = create_job(session, "validation", {}, owner_user_id="local")
    mark_done(session, job.job_id, {"excel_report": str(fake_report),
                                     "report_url": f"/api/validations/{job.job_id}/report"})
    session.close()

    response = client.get(f"/api/validations/{job.job_id}/report", headers=AUTH)
    assert response.status_code == 200
    assert response.content == b"fake xlsx bytes"
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_report_download_404s_if_the_file_is_missing_despite_a_done_job():
    session = _session()
    job = create_job(session, "validation", {}, owner_user_id="local")
    mark_done(session, job.job_id, {"excel_report": "/nonexistent/path/report.xlsx"})
    session.close()

    response = client.get(f"/api/validations/{job.job_id}/report", headers=AUTH)
    assert response.status_code == 404


def test_report_url_in_a_completed_job_points_at_the_download_endpoint():
    session = _session()
    job = create_job(session, "validation", {}, owner_user_id="local")
    mark_done(session, job.job_id, {"excel_report": "/x.xlsx",
                                     "report_url": f"/api/validations/{job.job_id}/report"})
    session.close()

    response = client.get(f"/api/validations/{job.job_id}", headers=AUTH)
    assert response.json()["report_url"] == f"/api/validations/{job.job_id}/report"
