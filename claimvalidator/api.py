"""HTTP API. Ontology endpoints let a UI pre-warm or inspect an ontology
independently; the validation endpoints are the core feature, async by
design — see jobs.py for why that matters and what the source repo's
equivalent route actually does instead.
"""

import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from db.database import get_session, init_database
from db.models import Job

from claimvalidator import config
from claimvalidator.document_identity import resolve_ontology_key
from claimvalidator.jobs import create_job, get_job, run_validation_job
from claimvalidator.schemas import OntologyBuildRequest, ValidationRequest

logger = logging.getLogger(__name__)

app = FastAPI(title="Claim Validator")

_SessionLocal, _engine = init_database()


def _session():
    return get_session(_SessionLocal)


@app.middleware("http")
async def require_api_token(request, call_next):
    from phases.api_auth import authorised

    if not authorised(request):
        return JSONResponse(
            status_code=401,
            content={
                "detail": "This API requires a token. Send it as `Authorization: Bearer …` "
                          "or an X-API-Token header. The token is printed when the server starts."
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


@app.get("/api/ping")
def ping():
    return {"status": "ok"}


# ---------------------------------------------------------------- ontologies

@app.post("/api/ontologies", status_code=202)
def build_ontology(request: OntologyBuildRequest, background: BackgroundTasks):
    from phases.ontology_store import OntologyStore

    store = OntologyStore(root=config.STORE_ROOT)
    key, reused = resolve_ontology_key(store, request.files, request.document_id)
    if reused or store.has_index(key):
        return {"key": key, "reused": True}

    session = _session()
    job = create_job(session, "ontology_build", request.model_dump())
    session.close()

    def _build():
        s = _session()
        try:
            from claimvalidator.jobs import mark_done, mark_failed, mark_running
            from phases.phase1_orchestrator import run_phase1

            mark_running(s, job.job_id)
            run_phase1(
                workflow_id=job.workflow_id,
                name=request.document_id or key,
                document_paths=request.files,
                llm_client=config.llm_client_factory(),
                store=store,
                ontology_key=key,
                background_description=request.background_description,
                output_dir=config.OUTPUT_DIR,
                db_session=s,
            )
            mark_done(s, job.job_id, {"key": key})
        except Exception as e:
            logger.exception(f"Ontology build {job.job_id} failed")
            from claimvalidator.jobs import mark_failed
            mark_failed(s, job.job_id, str(e))
        finally:
            s.close()

    background.add_task(_build)
    return {"key": key, "reused": False, "job_id": job.job_id}


@app.get("/api/ontologies/{key}")
def get_ontology(key: str):
    from phases.ontology_store import OntologyStore

    store = OntologyStore(root=config.STORE_ROOT)
    meta = store.load_meta(key)
    if not meta:
        raise HTTPException(404, f"No ontology {key!r}")
    current = store.load_current(key)
    return {"meta": meta.to_dict(), "has_index": store.has_index(key),
            "concept_types": len((current or {}).get("concept_types", []))}


@app.get("/api/ontologies/{key}/status")
def ontology_status(key: str):
    # No JSON-path query here — SQLAlchemy's generic JSON type doesn't offer
    # one portably across SQLite/Postgres, and job volume for this endpoint
    # is low enough that filtering in Python is the honest simple choice
    # rather than a backend-specific query that only works on one of them.
    session = _session()
    try:
        recent_builds = (session.query(Job)
                          .filter(Job.kind == "ontology_build")
                          .order_by(Job.created_at.desc())
                          .limit(200).all())
        for job in recent_builds:
            if (job.result_json or {}).get("key") == key:
                return {"status": job.status, "job_id": job.job_id}

        from phases.ontology_store import OntologyStore
        store = OntologyStore(root=config.STORE_ROOT)
        if store.has_index(key):
            return {"status": "done", "reused": True}
        raise HTTPException(404, f"No build job found for {key!r}")
    finally:
        session.close()


@app.delete("/api/ontologies/{key}")
def delete_ontology(key: str):
    from phases.ontology_store import OntologyStore

    store = OntologyStore(root=config.STORE_ROOT)
    if not store.load_meta(key):
        raise HTTPException(404, f"No ontology {key!r}")
    store.delete(key)
    return {"deleted": key}


# ---------------------------------------------------------------- validations

@app.post("/api/validations", status_code=202)
def submit_validation(request: ValidationRequest, background: BackgroundTasks):
    session = _session()
    job = create_job(session, "validation", request.model_dump(), request.webhook_url)
    session.close()

    background.add_task(
        run_validation_job, job.job_id, _SessionLocal, config.llm_client_factory,
    )
    return {"job_id": job.job_id, "status": "queued"}


@app.get("/api/validations/{job_id}")
def get_validation(job_id: str):
    session = _session()
    try:
        job = get_job(session, job_id)
        if not job:
            raise HTTPException(404, f"No job {job_id!r}")
        response: Dict[str, Any] = {"job_id": job_id, "status": job.status}
        if job.status == "done":
            response.update(job.result_json or {})
        elif job.status == "failed":
            response["error"] = job.error_message
        return response
    finally:
        session.close()


@app.get("/api/validations/{job_id}/events")
def get_validation_events(job_id: str):
    from db.models import PhaseEvent, PhaseExecution

    session = _session()
    try:
        job = get_job(session, job_id)
        if not job:
            raise HTTPException(404, f"No job {job_id!r}")
        executions = (session.query(PhaseExecution)
                      .filter(PhaseExecution.workflow_id == job.workflow_id).all())
        exec_ids = [e.phase_execution_id for e in executions]
        events = (session.query(PhaseEvent)
                  .filter(PhaseEvent.phase_execution_id.in_(exec_ids))
                  .order_by(PhaseEvent.timestamp).all())
        return {
            "job_id": job_id,
            "phases": [{"phase_name": e.phase_name, "status": e.status,
                        "started_at": e.started_at.isoformat() if e.started_at else None,
                        "completed_at": e.completed_at.isoformat() if e.completed_at else None}
                       for e in executions],
            "events": [{"event_type": ev.event_type, "timestamp": ev.timestamp.isoformat(),
                        "severity": ev.severity, "details": ev.details}
                       for ev in events],
        }
    finally:
        session.close()
