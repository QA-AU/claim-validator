"""API-layer smoke tests: the auth gate and the one always-public route.

Does not exercise the actual validation pipeline (that's covered by
scripts/validate_claims.py against a real model, and by pipeline.py's own
building blocks in the other test files) — this only proves the HTTP layer
wires request/response correctly and that the auth middleware fails closed.
"""

import os
import tempfile

# api.py opens the DB at import time, so the env var must be set first —
# a fixture running after import would be too late.
_tmp_db = tempfile.mkdtemp()
os.environ["CLAIMVAL_DB_URL"] = f"sqlite:///{_tmp_db}/test_api.db"
os.environ["CLAIMVAL_API_TOKEN"] = "test-suite-token"

from fastapi.testclient import TestClient  # noqa: E402

from claimvalidator.api import app  # noqa: E402
from phases.api_auth import reset_token  # noqa: E402

reset_token("test-suite-token")

client = TestClient(app)


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


def test_submit_validation_returns_immediately_with_a_job_id():
    response = client.post(
        "/api/validations",
        headers={"Authorization": "Bearer test-suite-token"},
        json={
            "document": {"document_id": "test-doc", "files": []},
            "claims": [{"id": "C1", "text": "a claim"}],
        },
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["job_id"].startswith("job_")


def test_get_unknown_validation_job_404s():
    response = client.get(
        "/api/validations/job_doesnotexist",
        headers={"Authorization": "Bearer test-suite-token"},
    )
    assert response.status_code == 404
