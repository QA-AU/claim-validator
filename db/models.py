"""SQLAlchemy ORM models for workflow state tracking."""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Current UTC time as a naive datetime.

    Replaces the deprecated `datetime.utcnow()` while storing exactly what it
    stored. The columns below are naive `DateTime`, so handing them an aware
    value would change what lands in the database and how existing rows compare
    against new ones — a migration, not a deprecation fix.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Declarative base for all models."""

    pass


class Workflow(Base):
    """Represents a complete workflow run across all phases."""

    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(100), unique=True)
    api_name: Mapped[str] = mapped_column(String(255))
    current_phase: Mapped[str] = mapped_column(String(50))  # phase1, phase2, etc.
    overall_status: Mapped[str] = mapped_column(String(50))  # pending, running, completed, failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    workflow_metadata: Mapped[dict] = mapped_column(JSON, nullable=True)

    def __repr__(self):
        return f"<Workflow {self.workflow_id} api={self.api_name} phase={self.current_phase}>"


class PhaseExecution(Base):
    """Tracks execution of a single phase within a workflow."""

    __tablename__ = "phase_executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    phase_execution_id: Mapped[str] = mapped_column(String(100), unique=True)
    workflow_id: Mapped[str] = mapped_column(String(100))
    phase_name: Mapped[str] = mapped_column(String(50))  # phase1, phase2, etc.
    status: Mapped[str] = mapped_column(String(50))  # pending, running, success, failed
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    error_message: Mapped[str] = mapped_column(Text, nullable=True)

    def __repr__(self):
        return f"<PhaseExecution {self.phase_name} workflow={self.workflow_id} status={self.status}>"


class PhaseEvent(Base):
    """Audit trail of events during phase execution."""

    __tablename__ = "phase_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(100), unique=True)
    phase_execution_id: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(50))  # step_start, step_complete, error, etc.
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    details: Mapped[dict] = mapped_column(JSON, nullable=True)
    severity: Mapped[str] = mapped_column(String(50))  # info, warning, error

    def __repr__(self):
        return f"<PhaseEvent {self.event_type} phase_exec={self.phase_execution_id}>"


class Artifact(Base):
    """Versioned outputs from each phase."""

    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(100), unique=True)
    phase_execution_id: Mapped[str] = mapped_column(String(100))
    artifact_type: Mapped[str] = mapped_column(String(50))  # ontology, rag_index, requirements, etc.
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self):
        return f"<Artifact {self.artifact_type} v{self.version} phase_exec={self.phase_execution_id}>"


class AskLog(Base):
    """One question asked of an ontology, and the answer given.

    Kept as its own table rather than folded into `UserInteraction`, which is
    keyed by workflow: a question is asked of an *ontology*, which outlives any
    single run, and the interesting columns here (retrieved chunks, citations,
    grounding score) have no meaning for a quality-gate interaction.

    Stored because an answer is a claim about a document. Being able to go back
    and see which passages produced it — months later, after the document has
    changed — is the same argument that made per-instance provenance worth
    building.
    """

    __tablename__ = "ask_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ask_id: Mapped[str] = mapped_column(String(100), unique=True)
    ontology_key: Mapped[str] = mapped_column(String(200))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, nullable=True)
    # Full claim list with each claim's chunk and instance ids.
    claims: Mapped[dict] = mapped_column(JSON, nullable=True)
    chunks_retrieved: Mapped[dict] = mapped_column(JSON, nullable=True)
    retrieval_score: Mapped[float] = mapped_column(default=0.0)
    claim_count: Mapped[int] = mapped_column(Integer, default=0)
    uncited_count: Mapped[int] = mapped_column(Integer, default=0)
    review_flags: Mapped[dict] = mapped_column(JSON, nullable=True)
    model: Mapped[str] = mapped_column(String(200), nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self):
        return f"<AskLog {self.ontology_key} q={self.question[:40]!r}>"


class UserInteraction(Base):
    """User feedback/questions at quality gates."""

    __tablename__ = "user_interactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    interaction_id: Mapped[str] = mapped_column(String(100), unique=True)
    workflow_id: Mapped[str] = mapped_column(String(100))
    phase_name: Mapped[str] = mapped_column(String(50))
    question: Mapped[str] = mapped_column(Text)
    feedback: Mapped[str] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self):
        return f"<UserInteraction workflow={self.workflow_id} phase={self.phase_name}>"


class ProcessPrompt(Base):
    """The core instruction for a named process, stored as data.

    Phase 2's prompt lived in `profiles/*.json` — version controlled, diffable,
    and pinned by the commit a run happened on. Moving it here buys runtime
    editing and an audit trail of who changed what, and costs the property that
    made the file version safe: a mutable row means the same input can produce
    different output with nothing recording why.

    So the row carries a `version`, and every run records the version it used.
    Rows are never edited in place — a change writes a new version and
    deactivates the old — which keeps the history a run can point back to.

    Files remain the fallback. The command-line tools run with no database at
    all (todo/07), and a prompt reachable only through a database would make the
    database a dependency of extraction rather than of auditing.

    ### One table, two prompt shapes

    The named columns below are Phase 2's parts, because Phase 2 is what this
    table was built for. Phase 3's prompt is not shaped that way — it has a task
    line, a list of rules and the framework's own instructions, and no
    categories or id format at all. Rather than give Phase 3 its own table, or
    file its rules under a column called `categories` and leave a reader to work
    out that the name is a lie, the extra parts go in `spec`.

    Phase 2's columns stay because they are queryable and because every existing
    row uses them. Nothing about versioning changes: a Phase 3 prompt is a row
    like any other, and a run records the version it read.
    """

    __tablename__ = "process_prompts"

    id: Mapped[int] = mapped_column(primary_key=True)
    # What this process is called, e.g. "api_testing_requirements".
    process_name: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Only one version of a process is active; the rest are history.
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # The prompt itself, in the parts the generator assembles.
    role: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text, default="")
    goal: Mapped[str] = mapped_column(Text, default="")
    categories: Mapped[list] = mapped_column(JSON, default=list)
    id_format: Mapped[str] = mapped_column(String(100), default="")

    # Parts belonging to a process whose prompt is not shaped like Phase 2's.
    # Overlaid on the columns above, so a row may use either or both.
    spec: Mapped[dict] = mapped_column(JSON, default=dict, nullable=True)

    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(100), default="")

    def to_spec(self) -> dict:
        """The prompt's parts, whichever shape this process uses.

        The named columns first, then `spec` over the top. A process that
        declares a part in both wins with `spec`, since that is the one it was
        written for.
        """
        spec = {
            "role": self.role,
            "subject": self.subject,
            "goal": self.goal,
            "categories": list(self.categories or []),
            "id_format": self.id_format,
        }
        spec.update(self.spec or {})
        return spec

    def __repr__(self):
        return f"<ProcessPrompt {self.process_name} v{self.version} active={self.active}>"


class ProcessSetting(Base):
    """A single tunable, stored as data and versioned like a prompt.

    Config that lives in code is pinned by the commit and invisible in the audit
    trail; config that lives in a mutable row is editable but cannot explain a
    past run. `ProcessPrompt` solved that for the instruction text by versioning
    and never editing in place, and a setting has exactly the same problem — a
    run that escalated to a stronger model must be able to say what the rule was
    at the time, not what it is now.

    So this is deliberately the same shape: one row per version, only one
    active, history kept. A run records the resolved value *and* its version, so
    "why did this run behave that way" always has an answer.

    Keys are namespaced by `process_name` (e.g. "entailment") so one table
    serves every phase rather than each growing its own.
    """

    __tablename__ = "process_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    process_name: Mapped[str] = mapped_column(String(100), index=True)
    key: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # JSON rather than a typed column: settings are booleans, ints and short
    # strings, and a value column per type would be three columns and a
    # discriminator for no gain.
    value: Mapped[dict] = mapped_column(JSON, default=dict)

    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(100), default="")

    def __repr__(self):
        return (
            f"<ProcessSetting {self.process_name}.{self.key} v{self.version} "
            f"active={self.active}>"
        )


class Job(Base):
    """An async validation or ontology-build request and its outcome.

    Added in claim-validator — not present in the repo `db/models.py` was
    copied from. `workflow_id` links a job's rows to the `PhaseExecution`
    rows `RunTracker` writes for it (`phase_execution_id` is derived as
    `f"{workflow_id}-{phase_name}"`), so a job's own status here and its
    step-by-step trail there are two views of the same run, not two records
    that can disagree.

    Status persisted here rather than only held in memory is what lets
    `GET /api/validations/{job_id}` answer correctly across a process
    restart, and what lets a background task's own failure be reported
    rather than silently disappearing with the process.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    workflow_id: Mapped[str] = mapped_column(String(100), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # "validation" | "ontology_build"
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued/running/done/failed

    request_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=True)
    webhook_url: Mapped[str] = mapped_column(String(500), nullable=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self):
        return f"<Job {self.job_id} kind={self.kind} status={self.status}>"
