"""Records a run's progress to the database, if there is one.

Progress in the UI is currently animated client-side, so a browser reload loses
all knowledge of a running extraction. Real `PhaseEvent` rows let the UI poll
actual state instead of guessing.

Two rules shape this module:

* **A tracking failure must never fail the run.** Extraction is the expensive
  part; losing it because an audit row would not insert is a bad trade. Every
  write is wrapped, and a failure is logged loudly rather than raised.
* **No session means a silent no-op.** The CLI, the tests and any embedding
  caller run without a database, and none of them should need to care.

Deliberately *not* silent about one thing: if a session is supplied and writes
fail, that is logged at error level every time. A database that is configured
but not working is a different situation from one that was never configured, and
the difference has to be visible.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

EVENT_STEP_START = "step_start"
EVENT_STEP_COMPLETE = "step_complete"
EVENT_ERROR = "error"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"


def _utcnow():
    from db.models import utcnow

    return utcnow()


class RunTracker:
    """Writes workflow, phase and event rows for one Phase 1 run."""

    def __init__(self, db_session, workflow_id: str, name: str, phase_name: str = "phase1"):
        self.session = db_session
        self.workflow_id = workflow_id
        self.name = name
        self.phase_name = phase_name
        self.phase_execution_id = f"{workflow_id}-{phase_name}"
        self._failed = False

    @property
    def enabled(self) -> bool:
        return self.session is not None and not self._failed

    def _guard(self, what: str, fn):
        if not self.enabled:
            return None
        try:
            return fn()
        except Exception as e:
            # Disable after the first failure rather than logging identically on
            # every subsequent step — one clear error, not forty.
            self._failed = True
            try:
                self.session.rollback()
            except Exception:
                pass
            logger.error(
                f"[Tracker] {what} failed; run continues without database tracking: {e}",
                exc_info=True,
            )
            return None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Open (or reopen) the workflow and its phase execution row."""

        def _start():
            from db.models import PhaseExecution, Workflow

            workflow = (
                self.session.query(Workflow).filter_by(workflow_id=self.workflow_id).first()
            )
            if workflow is None:
                workflow = Workflow(
                    workflow_id=self.workflow_id,
                    api_name=self.name,
                    current_phase=self.phase_name,
                    overall_status="running",
                )
                self.session.add(workflow)
            else:
                workflow.current_phase = self.phase_name
                workflow.overall_status = "running"

            execution = (
                self.session.query(PhaseExecution)
                .filter_by(phase_execution_id=self.phase_execution_id)
                .first()
            )
            if execution is None:
                execution = PhaseExecution(
                    phase_execution_id=self.phase_execution_id,
                    workflow_id=self.workflow_id,
                    phase_name=self.phase_name,
                    status="running",
                )
                self.session.add(execution)
            else:
                # A rerun of the same workflow: reset rather than leaving the
                # previous attempt's terminal status in place.
                execution.status = "running"
                execution.completed_at = None
                execution.error_message = None

            self.session.commit()

        self._guard("start", _start)

    def event(
        self,
        event_type: str,
        step: str,
        details: Optional[Dict[str, Any]] = None,
        severity: str = SEVERITY_INFO,
    ) -> None:
        """Record one step-level event."""

        def _event():
            from db.models import PhaseEvent

            payload = {"step": step}
            if details:
                payload.update(details)

            self.session.add(
                PhaseEvent(
                    event_id=str(uuid.uuid4()),
                    phase_execution_id=self.phase_execution_id,
                    event_type=event_type,
                    details=payload,
                    severity=severity,
                )
            )
            self.session.commit()

        self._guard(f"event({step})", _event)

    def step_start(self, step: str, **details) -> None:
        self.event(EVENT_STEP_START, step, details or None)

    def step_complete(self, step: str, **details) -> None:
        self.event(EVENT_STEP_COMPLETE, step, details or None)

    def error(self, step: str, message: str) -> None:
        self.event(EVENT_ERROR, step, {"message": message}, severity=SEVERITY_ERROR)

    def finish(
        self,
        status: str,
        tokens_used: int = 0,
        cost_cents: Optional[float] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Close out the phase execution and the workflow."""

        def _finish():
            from db.models import PhaseExecution, Workflow

            execution = (
                self.session.query(PhaseExecution)
                .filter_by(phase_execution_id=self.phase_execution_id)
                .first()
            )
            if execution is not None:
                execution.status = status
                execution.completed_at = _utcnow()
                execution.tokens_used = tokens_used
                # The column is USD; cost is tracked in cents everywhere else.
                # None means no prices were configured, which is not zero — but
                # the column is not nullable in practice, so 0.0 stands in and
                # `tokens_used` remains the honest figure.
                execution.cost_usd = (cost_cents / 100.0) if cost_cents is not None else 0.0
                execution.error_message = error_message

            workflow = (
                self.session.query(Workflow).filter_by(workflow_id=self.workflow_id).first()
            )
            if workflow is not None:
                workflow.overall_status = status
                workflow.updated_at = _utcnow()

            self.session.commit()

        self._guard("finish", _finish)

    def save_artifact(self, artifact_type: str, content: Dict[str, Any]) -> Optional[int]:
        """Store a versioned artifact, incrementing the version per type.

        Versions line up with the on-disk `versions/` directory: each run of a
        given type gets the next number rather than overwriting the last.
        """

        def _save():
            from db.models import Artifact

            latest = (
                self.session.query(Artifact)
                .filter_by(phase_execution_id=self.phase_execution_id, artifact_type=artifact_type)
                .order_by(Artifact.version.desc())
                .first()
            )
            version = (latest.version + 1) if latest else 1

            artifact = Artifact(
                artifact_id=str(uuid.uuid4()),
                phase_execution_id=self.phase_execution_id,
                artifact_type=artifact_type,
                version=version,
                content=content,
            )
            self.session.add(artifact)
            self.session.commit()
            return version

        return self._guard(f"save_artifact({artifact_type})", _save)

    def record_interaction(self, question: str, feedback: str) -> None:
        """Persist a user answer alongside the run that prompted it."""

        def _record():
            from db.models import UserInteraction

            self.session.add(
                UserInteraction(
                    interaction_id=str(uuid.uuid4()),
                    workflow_id=self.workflow_id,
                    phase_name=self.phase_name,
                    question=question,
                    feedback=feedback,
                )
            )
            self.session.commit()

        self._guard("record_interaction", _record)


def log_ask(
    db_session,
    ontology_key: str,
    answer,
    model: str = "",
    tokens_used: int = 0,
    duration_seconds: float = 0.0,
) -> Optional[str]:
    """Record one question and its answer. Returns the ask id, or None.

    Wrapped like every other write here: an answer that was produced must not be
    lost because the audit row would not insert. A logging failure is reported
    and the answer still goes back to the caller.
    """
    if db_session is None:
        return None

    try:
        from db.models import AskLog

        ask_id = str(uuid.uuid4())
        payload = answer.to_dict()
        db_session.add(
            AskLog(
                ask_id=ask_id,
                ontology_key=ontology_key,
                question=answer.question,
                answer=answer.answer_text,
                claims=payload["claims"],
                chunks_retrieved=payload["chunks_retrieved"],
                retrieval_score=float(answer.retrieval_score),
                claim_count=len(answer.claims),
                uncited_count=len(answer.uncited),
                review_flags=payload["review_flags"],
                model=model,
                tokens_used=tokens_used,
                duration_seconds=duration_seconds,
            )
        )
        db_session.commit()
        return ask_id
    except Exception as e:
        try:
            db_session.rollback()
        except Exception:
            pass
        logger.error(f"[Tracker] Could not log the question for {ontology_key}: {e}")
        return None


def ask_history(db_session, ontology_key: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Questions asked of one ontology, newest first."""
    if db_session is None:
        return []

    try:
        from db.models import AskLog

        rows = (
            db_session.query(AskLog)
            .filter_by(ontology_key=ontology_key)
            .order_by(AskLog.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "ask_id": r.ask_id,
                "question": r.question,
                "answer": r.answer,
                "claims": r.claims or [],
                "chunks_retrieved": r.chunks_retrieved or [],
                "retrieval_score": r.retrieval_score,
                "claim_count": r.claim_count,
                "uncited_count": r.uncited_count,
                "review_flags": r.review_flags or [],
                "model": r.model,
                "tokens_used": r.tokens_used,
                "duration_seconds": r.duration_seconds,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"[Tracker] Could not read question history for {ontology_key}: {e}")
        return []


def run_status(db_session, workflow_id: str) -> Optional[Dict[str, Any]]:
    """Read a run's state back from the database.

    This is what makes status survive a restart: the in-memory results dict is
    process-local and is lost whenever the server bounces or runs more than one
    worker.
    """
    if db_session is None:
        return None

    try:
        from db.models import PhaseEvent, PhaseExecution, Workflow

        workflow = db_session.query(Workflow).filter_by(workflow_id=workflow_id).first()
        if workflow is None:
            return None

        execution = (
            db_session.query(PhaseExecution)
            .filter_by(workflow_id=workflow_id)
            .order_by(PhaseExecution.started_at.desc())
            .first()
        )

        events = []
        if execution is not None:
            events = (
                db_session.query(PhaseEvent)
                .filter_by(phase_execution_id=execution.phase_execution_id)
                .order_by(PhaseEvent.timestamp.asc())
                .all()
            )

        return {
            "workflow_id": workflow.workflow_id,
            "name": workflow.api_name,
            "current_phase": workflow.current_phase,
            "status": workflow.overall_status,
            "created_at": workflow.created_at.isoformat() if workflow.created_at else None,
            "updated_at": workflow.updated_at.isoformat() if workflow.updated_at else None,
            "phase": None
            if execution is None
            else {
                "name": execution.phase_name,
                "status": execution.status,
                "tokens_used": execution.tokens_used,
                "cost_usd": execution.cost_usd,
                "error_message": execution.error_message,
            },
            "events": [
                {
                    "type": e.event_type,
                    "severity": e.severity,
                    "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                    "details": e.details or {},
                }
                for e in events
            ],
        }
    except Exception as e:
        logger.error(f"[Tracker] Could not read run status for {workflow_id}: {e}")
        return None
