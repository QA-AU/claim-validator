"""run_validation_job's wiring to pipeline.run_validation.

Found live: this call was missing store_root and output_dir entirely,
so every validation submitted via POST /api/validations silently used
run_validation's own relative defaults ("./.data/ontologies") instead
of config.STORE_ROOT (the actual mounted, persisted share in
production) — an ontology built this way looked fine within the same
container process, then vanished the moment that container restarted,
since it was never actually on persisted storage. No test caught it
because run_validation_job itself had zero test coverage. This file
exists specifically to make sure that gap can't reopen silently.
"""

import pytest

from claimvalidator import config
from claimvalidator.jobs import create_job, run_validation_job
from claimvalidator.pipeline import ValidationResult
from db.database import init_database


@pytest.fixture
def session_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAIMVAL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    SessionLocal, _ = init_database()
    return SessionLocal


def test_run_validation_job_passes_configured_store_root_and_output_dir(
    session_factory, monkeypatch
):
    captured = {}

    def fake_run_validation(**kwargs):
        captured.update(kwargs)
        return ValidationResult(ontology_key="k", ontology_reused=True)

    monkeypatch.setattr("claimvalidator.pipeline.run_validation", fake_run_validation)
    monkeypatch.setattr(
        "claimvalidator.report_excel.build_excel_report", lambda result, path: None
    )

    session = session_factory()
    job = create_job(session, "validation", {
        "document": {"document_id": "d", "files": []},
        "claims": [{"id": "C1", "text": "x"}],
    })
    session.close()

    run_validation_job(job.job_id, session_factory, lambda: object())

    assert captured["store_root"] == config.STORE_ROOT
    assert captured["output_dir"] == config.OUTPUT_DIR
