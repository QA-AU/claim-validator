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
    job = create_job(session, "validation", {})
    mark_running(session, job.job_id)
    session.close()

    response = client.get(f"/api/validations/{job.job_id}/report", headers=AUTH)
    assert response.status_code == 409


def test_report_download_streams_the_real_file(tmp_path):
    fake_report = tmp_path / "report.xlsx"
    fake_report.write_bytes(b"fake xlsx bytes")

    session = _session()
    job = create_job(session, "validation", {})
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
    job = create_job(session, "validation", {})
    mark_done(session, job.job_id, {"excel_report": "/nonexistent/path/report.xlsx"})
    session.close()

    response = client.get(f"/api/validations/{job.job_id}/report", headers=AUTH)
    assert response.status_code == 404


def test_report_url_in_a_completed_job_points_at_the_download_endpoint():
    session = _session()
    job = create_job(session, "validation", {})
    mark_done(session, job.job_id, {"excel_report": "/x.xlsx",
                                     "report_url": f"/api/validations/{job.job_id}/report"})
    session.close()

    response = client.get(f"/api/validations/{job.job_id}", headers=AUTH)
    assert response.json()["report_url"] == f"/api/validations/{job.job_id}/report"
