"""HTTP API. Ontology endpoints let a UI pre-warm or inspect an ontology
independently; the validation endpoints are the core feature, async by
design — see jobs.py for why that matters and what the source repo's
equivalent route actually does instead.
"""

import logging
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from db.database import get_session, init_database
from db.models import Job

from claimvalidator import config
from claimvalidator.document_identity import resolve_ontology_key
from claimvalidator.jobs import create_job, get_job, run_validation_job
from claimvalidator.schemas import OntologyBuildRequest, ValidationRequest

logger = logging.getLogger(__name__)

app = FastAPI(title="Claim Validator")

# Caller-supplied document_id becomes a directory name (see upload_document
# below) — restricted to a safe charset so it can never carry a path
# separator or ".." out of config.SOURCE_DIR.
_SAFE_DOCUMENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_SessionLocal, _engine = init_database()


def _session():
    return get_session(_SessionLocal)


@app.middleware("http")
async def require_api_token(request, call_next):
    # Path gating (what's public at all) and token extraction are shared
    # between the two auth backends; only how the token itself gets
    # checked differs. azure_auth.enabled() being false is what keeps
    # local dev and the test suite working exactly as before — nothing
    # here changes unless CLAIMVAL_AZURE_TENANT_ID is actually set.
    from phases.api_auth import is_public, presented_token, token_matches
    from claimvalidator import azure_auth

    if is_public(request.url.path):
        return await call_next(request)

    token = presented_token(request.headers, request.cookies)

    if azure_auth.enabled():
        claims = azure_auth.validate(token or "")
        if claims is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing Azure AD access token."},
                headers={"WWW-Authenticate": "Bearer"},
            )
        # `oid` is the stable per-user Entra ID identifier; `sub` as a
        # fallback for a token shape that omits it. This is what every job
        # and freshly-built ontology gets attributed to below — see
        # jobs.py::get_job and ontology_store.py's created_by field.
        request.state.user_id = claims.get("oid") or claims.get("sub") or "unknown"
    elif not token_matches(token):
        return JSONResponse(
            status_code=401,
            content={
                "detail": "This API requires a token. Send it as `Authorization: Bearer …` "
                          "or an X-API-Token header. The token is printed when the server starts."
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    else:
        # No per-user identity exists in shared-secret mode — one fixed
        # sentinel, so job/ontology ownership filtering stays a plain
        # equality check either way rather than needing a NULL special case.
        request.state.user_id = "local"
    return await call_next(request)


@app.get("/api/ping")
def ping():
    return {"status": "ok"}


# ---------------------------------------------------------------- documents

@app.post("/api/documents", status_code=201)
async def upload_document(file: UploadFile = File(...), document_id: Optional[str] = Form(None)):
    """Upload a reference document, get back a document_id to pass as
    document.files in POST /api/validations. Writes to config.SOURCE_DIR —
    a local path in dev, a mounted Azure Files share in production, with no
    code difference between the two.

    document_id is a caller-supplied label, not the real identity key —
    resolve_ontology_key already hashes document content for that, the same
    way it does for the ontology-build path. Two uploads of the same bytes
    under different document_ids still share one cached ontology.
    """
    from phases.phase1_document_loader import validate_file_type

    if not file.filename or not validate_file_type(file.filename):
        suffix = Path(file.filename or "").suffix.lower()
        raise HTTPException(400, f"Unsupported file type: {suffix or '(none)'}. "
                                  f"See phases/phase1_document_loader.py for supported formats.")

    if document_id is not None and not _SAFE_DOCUMENT_ID.match(document_id):
        raise HTTPException(400, "document_id may only contain letters, digits, "
                                  "underscores, and hyphens.")

    # file.filename is caller-supplied and untrusted — .name strips any
    # directory component (including "../" traversal or an absolute path)
    # so only ever the bare filename reaches the filesystem.
    safe_filename = Path(file.filename).name
    if not safe_filename:
        raise HTTPException(400, "Invalid filename.")

    doc_id = document_id or f"doc_{uuid.uuid4().hex[:12]}"
    dest_dir = Path(config.SOURCE_DIR) / doc_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / safe_filename

    size = 0
    with open(dest_path, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > config.MAX_UPLOAD_BYTES:
                out.close()
                dest_path.unlink(missing_ok=True)
                try:
                    dest_dir.rmdir()  # only succeeds if empty — leaves a
                                      # caller-reused document_id's other
                                      # files alone, only cleans up the
                                      # directory this rejected upload
                                      # itself just created
                except OSError:
                    pass
                raise HTTPException(
                    413, f"File exceeds the {config.MAX_UPLOAD_BYTES:,}-byte upload limit"
                )
            out.write(chunk)

    return {"document_id": doc_id, "filename": safe_filename, "path": str(dest_path),
            "size_bytes": size}


# ---------------------------------------------------------------- ontologies

@app.post("/api/ontologies", status_code=202)
def build_ontology(request: OntologyBuildRequest, http_request: Request, background: BackgroundTasks):
    from phases.ontology_store import OntologyStore

    store = OntologyStore(root=config.STORE_ROOT)
    owner_user_id = http_request.state.user_id
    key, reused = resolve_ontology_key(
        store, request.files, request.document_id, created_by=owner_user_id
    )
    if reused or store.has_index(key):
        return {"key": key, "reused": True}

    session = _session()
    job = create_job(session, "ontology_build", request.model_dump(), owner_user_id=owner_user_id)
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


@app.get("/api/ontologies")
def list_ontologies():
    """Every ontology that exists — shared and browsable by any
    authenticated user, since ontologies aren't scoped to whoever built
    them (only jobs and reports are private; see get_validation below).

    Reads each ontology's current.json to report concept_types, the same
    way GET /api/ontologies/{key} already does for one. Fine at the scale
    this is meant for (tens of ontologies, not thousands) — not paginated
    or cached in this pass.
    """
    from phases.ontology_store import OntologyStore

    store = OntologyStore(root=config.STORE_ROOT)
    ontologies = []
    for meta in store.list():
        current = store.load_current(meta.key)
        ontologies.append({
            "key": meta.key,
            "name": meta.name,
            "created_by": meta.created_by,
            "created_at": meta.created_at,
            "content_hash": meta.content_hash,
            "has_index": store.has_index(meta.key),
            "concept_types": len((current or {}).get("concept_types", [])),
        })
    return {"ontologies": ontologies}


@app.get("/api/ontologies/{key}")
def get_ontology(key: str):
    from phases.ontology_store import OntologyStore

    store = OntologyStore(root=config.STORE_ROOT)
    try:
        meta = store.load_meta(key)
    except ValueError:
        # A malformed key (path.for()'s charset check) is just as
        # not-found as one that's well-formed but unknown — same response
        # either way, so a caller learns nothing about why it failed.
        meta = None
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
        try:
            has_index = store.has_index(key)
        except ValueError:
            has_index = False
        if has_index:
            return {"status": "done", "reused": True}
        raise HTTPException(404, f"No build job found for {key!r}")
    finally:
        session.close()


# Deliberately no DELETE /api/ontologies/{key} in the shared model: an
# ontology is a shared, immutable asset once built, and one authenticated
# user shouldn't be able to destroy something another user has started
# relying on. OntologyStore.delete() still exists as a lower-level method
# for operator/migration use — only the HTTP route is gone.


# ---------------------------------------------------------------- validations

@app.post("/api/validations", status_code=202)
def submit_validation(request: ValidationRequest, http_request: Request, background: BackgroundTasks):
    if not request.ontology_key and not request.document.files:
        raise HTTPException(
            400, "Provide either document.files (to build or auto-reuse an ontology) "
                 "or ontology_key (to validate against an existing one directly)."
        )

    owner_user_id = http_request.state.user_id
    session = _session()
    job = create_job(session, "validation", request.model_dump(), request.webhook_url,
                      owner_user_id=owner_user_id)
    session.close()

    background.add_task(
        run_validation_job, job.job_id, _SessionLocal, config.llm_client_factory,
    )
    return {"job_id": job.job_id, "status": "queued"}


@app.get("/api/validations/{job_id}")
def get_validation(job_id: str, http_request: Request):
    session = _session()
    try:
        job = get_job(session, job_id, owner_user_id=http_request.state.user_id)
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
def get_validation_events(job_id: str, http_request: Request):
    from db.models import PhaseEvent, PhaseExecution

    session = _session()
    try:
        job = get_job(session, job_id, owner_user_id=http_request.state.user_id)
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


@app.get("/api/validations/{job_id}/report")
def get_validation_report(job_id: str, http_request: Request):
    """Streams the report from wherever it actually lives (local disk in
    dev, a mounted Azure Files share in production) — the caller never
    needs to know that path, only this URL, which is what result_json's
    report_url now points at instead of the bare local path it used to be."""
    session = _session()
    try:
        job = get_job(session, job_id, owner_user_id=http_request.state.user_id)
        if not job:
            raise HTTPException(404, f"No job {job_id!r}")
        if job.status != "done":
            raise HTTPException(409, f"Job {job_id!r} is {job.status!r}, not done yet")

        report_path = (job.result_json or {}).get("excel_report")
        if not report_path or not Path(report_path).exists():
            raise HTTPException(404, f"No report file found for job {job_id!r}")

        return FileResponse(
            report_path,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=f"{job_id}.xlsx",
        )
    finally:
        session.close()
