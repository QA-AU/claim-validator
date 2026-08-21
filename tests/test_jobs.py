"""Job persistence — create/read/status transitions, no pipeline involved."""

import pytest

from claimvalidator.jobs import create_job, get_job, mark_done, mark_failed, mark_running
from db.database import init_database


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAIMVAL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    SessionLocal, _ = init_database()
    s = SessionLocal()
    yield s
    s.close()


def test_create_job_starts_queued(session):
    job = create_job(session, "validation", {"claims": []})
    assert job.status == "queued"
    assert job.job_id.startswith("job_")
    assert job.workflow_id.startswith("validation-")


def test_get_job_survives_a_new_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAIMVAL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    SessionLocal, _ = init_database()

    s1 = SessionLocal()
    job = create_job(s1, "validation", {"claims": []})
    s1.close()

    # A fresh session, as a restarted process would open — proves status is
    # actually persisted, not just held in the object that created it.
    s2 = SessionLocal()
    fetched = get_job(s2, job.job_id)
    assert fetched is not None
    assert fetched.status == "queued"
    s2.close()


def test_mark_running_then_done(session):
    job = create_job(session, "validation", {"claims": []})
    mark_running(session, job.job_id)
    assert get_job(session, job.job_id).status == "running"

    mark_done(session, job.job_id, {"entailed": 1})
    done = get_job(session, job.job_id)
    assert done.status == "done"
    assert done.result_json == {"entailed": 1}


def test_mark_failed_records_the_error(session):
    job = create_job(session, "validation", {"claims": []})
    mark_failed(session, job.job_id, "simulated failure")
    failed = get_job(session, job.job_id)
    assert failed.status == "failed"
    assert failed.error_message == "simulated failure"


def test_get_job_returns_none_for_unknown_id(session):
    assert get_job(session, "job_doesnotexist") is None
