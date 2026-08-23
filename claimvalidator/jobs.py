"""Real async job handling — not the pattern found in the source repo's
`phase1_web_app.py`, whose `/api/extract` route calls `run_phase1` directly
inside the request handler despite having a `/status` polling endpoint: the
appearance of async without the substance, confirmed by reading the route.

Here, `POST /api/validations` returns as soon as a `Job` row exists;
`run_validation_job` (called from FastAPI's `BackgroundTasks`) does the
actual multi-minute work and updates that same row when it's done.
"""

import logging
import time
import uuid
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from db.models import Job

logger = logging.getLogger(__name__)


def new_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:12]}"


def create_job(session: Session, kind: str, request_json: Dict[str, Any],
               webhook_url: Optional[str] = None) -> Job:
    job = Job(
        job_id=new_job_id(),
        workflow_id=f"{kind}-{uuid.uuid4().hex[:10]}",
        kind=kind,
        status="queued",
        request_json=request_json,
        webhook_url=webhook_url,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def get_job(session: Session, job_id: str) -> Optional[Job]:
    return session.query(Job).filter(Job.job_id == job_id).first()


def mark_running(session: Session, job_id: str) -> None:
    session.query(Job).filter(Job.job_id == job_id).update({"status": "running"})
    session.commit()


def mark_done(session: Session, job_id: str, result_json: Dict[str, Any]) -> None:
    session.query(Job).filter(Job.job_id == job_id).update({
        "status": "done", "result_json": result_json,
    })
    session.commit()


def mark_failed(session: Session, job_id: str, error_message: str) -> None:
    session.query(Job).filter(Job.job_id == job_id).update({
        "status": "failed", "error_message": error_message,
    })
    session.commit()


def run_validation_job(job_id: str, SessionLocal, llm_client_factory) -> None:
    """The actual work, run off the request thread by `BackgroundTasks`.

    Takes a session factory rather than a session — the request's own session
    is closed by the time this runs, since the handler already returned.
    Takes an `llm_client_factory` rather than a client for the same reason a
    fresh client per background task is safer than sharing one across
    concurrent jobs whose usage tracking (`client.usage`) would otherwise mix.
    """
    from claimvalidator import config
    from claimvalidator.pipeline import run_validation
    from claimvalidator.report_excel import build_excel_report
    from claimvalidator.schemas import ValidationRequest

    session = SessionLocal()
    try:
        job = get_job(session, job_id)
        if job is None:
            logger.error(f"run_validation_job: no such job {job_id}")
            return

        mark_running(session, job_id)
        request = ValidationRequest.model_validate(job.request_json)
        llm_client = llm_client_factory()

        started = time.monotonic()
        result = run_validation(
            workflow_id=job.workflow_id,
            document_paths=request.document.files,
            claims_input=[c.model_dump() for c in request.claims],
            llm_client=llm_client,
            document_id=request.document.document_id,
            db_session=session,
            shape_rule_overrides=request.options.shape_rules,
            census_max_chunks=request.options.census_max_chunks,
            force_census=request.options.force_census,
        )
        duration_s = time.monotonic() - started

        # The one artefact meant for a person to open, written next to the
        # JSON result rather than left as something only a script produces —
        # this was previously true only when someone called
        # build_excel_report by hand; a job run through the API never
        # actually wrote one.
        excel_path = f"{config.REPORTS_DIR}/{job_id}.xlsx"
        build_excel_report(result, excel_path)

        result_dict = result.to_dict()
        result_dict["job_id"] = job_id
        result_dict["duration_seconds"] = round(duration_s, 1)
        # excel_report is the server's own local (or mounted-share) path —
        # a caller can't do anything with it directly. report_url is the
        # one a caller should actually use; kept both since the download
        # endpoint itself reads excel_report server-side to find the file.
        result_dict["excel_report"] = excel_path
        result_dict["report_url"] = f"/api/validations/{job_id}/report"
        mark_done(session, job_id, result_dict)

        if job.webhook_url:
            from claimvalidator.webhooks import deliver
            deliver(job.webhook_url, {"job_id": job_id, "status": "done"})

    except Exception as e:
        logger.exception(f"Validation job {job_id} failed")
        mark_failed(session, job_id, str(e))
        job = get_job(session, job_id)
        if job and job.webhook_url:
            from claimvalidator.webhooks import deliver
            deliver(job.webhook_url, {"job_id": job_id, "status": "failed", "error": str(e)})
    finally:
        session.close()
