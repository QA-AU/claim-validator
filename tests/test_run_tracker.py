"""Tests for database run tracking.

Two properties. A run must be recorded — including its failures, or a crashed
run is indistinguishable from one still in progress forever. And tracking must
never be able to fail the run: extraction is the expensive part, and losing it
because an audit row would not insert is a bad trade.

The regression this locks down: `save_to_database()` imported `ExecutionStage`
and `WorkflowExecution`, which have never existed in `db/models.py`. An
`except ImportError` turned that into a warning, so the function appeared to
work and wrote nothing.
"""

import json

import pytest

from db.database import init_database
from db.models import Artifact, PhaseEvent, PhaseExecution, UserInteraction, Workflow
from phases.llm_usage import UsageTrackingMixin
from phases.run_tracker import RunTracker, run_status


@pytest.fixture
def session(tmp_path, monkeypatch):
    # The local database path is resolved relative to db/database.py, not the
    # working directory, so chdir does not isolate it — without this override
    # every test would share (and pollute) the developer's real database.
    monkeypatch.setenv("CLAIMVAL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    SessionLocal, _ = init_database()
    session = SessionLocal()
    yield session
    session.close()


class Stub(UsageTrackingMixin):
    def generate(self, prompt):
        self.usage.record(100, 20)
        low = prompt.lower()
        if "decide what kinds of" in low:
            return json.dumps(
                [{"name": "endpoint", "description": "d", "surface_terms": ["endpoint"], "attributes": []}]
            )
        if prompt.strip().lower().startswith("extract every instance"):
            return json.dumps([{"name": "/orders", "attributes": {}, "source_chunk": 0}])
        return "[]"


class TestTheModelsExist:
    def test_the_names_the_tracker_uses_are_real(self):
        """The old save path imported two classes that were never in the schema."""
        from db import models

        for name in ("Workflow", "PhaseExecution", "PhaseEvent", "Artifact", "UserInteraction"):
            assert hasattr(models, name), f"db.models.{name} is missing"

    def test_the_dead_names_are_still_absent(self):
        from db import models

        assert not hasattr(models, "ExecutionStage")
        assert not hasattr(models, "WorkflowExecution")


class TestRecording:
    def test_a_run_creates_a_workflow_and_phase_row(self, session):
        tracker = RunTracker(session, "wf-1", "Orders API")
        tracker.start()

        assert session.query(Workflow).filter_by(workflow_id="wf-1").first() is not None
        execution = session.query(PhaseExecution).filter_by(workflow_id="wf-1").first()
        assert execution.status == "running"

    def test_steps_are_recorded_as_events(self, session):
        tracker = RunTracker(session, "wf-1", "Orders API")
        tracker.start()
        tracker.step_start("index")
        tracker.step_complete("index", chunks=42)

        events = session.query(PhaseEvent).all()
        assert [e.event_type for e in events] == ["step_start", "step_complete"]
        assert events[1].details["chunks"] == 42

    def test_finishing_records_status_and_cost(self, session):
        tracker = RunTracker(session, "wf-1", "Orders API")
        tracker.start()
        tracker.finish(status="completed", tokens_used=1200, cost_cents=250.0)

        execution = session.query(PhaseExecution).filter_by(workflow_id="wf-1").first()
        assert execution.status == "completed"
        assert execution.tokens_used == 1200
        assert execution.cost_usd == pytest.approx(2.5)

    def test_unknown_cost_is_not_recorded_as_a_real_zero(self, session):
        """Tokens stay honest even when no prices are configured."""
        tracker = RunTracker(session, "wf-1", "Orders API")
        tracker.start()
        tracker.finish(status="completed", tokens_used=1200, cost_cents=None)

        execution = session.query(PhaseExecution).filter_by(workflow_id="wf-1").first()
        assert execution.tokens_used == 1200
        assert execution.cost_usd == 0.0

    def test_artifacts_are_versioned_not_overwritten(self, session):
        tracker = RunTracker(session, "wf-1", "Orders API")
        tracker.start()

        assert tracker.save_artifact("ontology", {"v": 1}) == 1
        assert tracker.save_artifact("ontology", {"v": 2}) == 2

        assert session.query(Artifact).count() == 2

    def test_user_answers_are_persisted(self, session):
        tracker = RunTracker(session, "wf-1", "Orders API")
        tracker.start()
        tracker.record_interaction("What is the rate limit?", "1000/hr — per contract")

        interaction = session.query(UserInteraction).first()
        assert interaction.workflow_id == "wf-1"
        assert "1000/hr" in interaction.feedback

    def test_a_rerun_resets_rather_than_leaving_a_stale_status(self, session):
        tracker = RunTracker(session, "wf-1", "Orders API")
        tracker.start()
        tracker.finish(status="failed", error_message="boom")

        RunTracker(session, "wf-1", "Orders API").start()

        execution = session.query(PhaseExecution).filter_by(workflow_id="wf-1").first()
        assert execution.status == "running"
        assert execution.error_message is None


class TestTrackingNeverBreaksTheRun:
    def test_no_session_is_a_silent_no_op(self):
        tracker = RunTracker(None, "wf-1", "Orders API")
        tracker.start()
        tracker.step_start("index")
        tracker.finish(status="completed")

        assert tracker.enabled is False

    def test_a_broken_session_disables_tracking_instead_of_raising(self):
        class Broken:
            def query(self, *a, **k):
                raise RuntimeError("database is gone")

            def add(self, *a, **k):
                raise RuntimeError("database is gone")

            def commit(self):
                raise RuntimeError("database is gone")

            def rollback(self):
                pass

        tracker = RunTracker(Broken(), "wf-1", "Orders API")
        tracker.start()  # must not raise

        assert tracker.enabled is False
        tracker.step_start("index")
        tracker.finish(status="completed")

    def test_a_run_completes_with_a_broken_database(self, tmp_path):
        """Losing a finished extraction because an audit row failed is a bad trade."""
        from phases.phase1_orchestrator import run_phase1

        class Broken:
            def query(self, *a, **k):
                raise RuntimeError("nope")

            def add(self, *a, **k):
                raise RuntimeError("nope")

            def commit(self):
                raise RuntimeError("nope")

            def rollback(self):
                pass

        source = tmp_path / "spec.txt"
        source.write_text("The orders endpoint is documented here.\n" * 40)

        output = run_phase1(
            workflow_id="wf-1",
            name="Orders API",
            document_paths=[str(source)],
            llm_client=Stub(),
            output_dir=str(tmp_path / "out"),
            db_session=Broken(),
        )

        assert output.status in ("success", "partial")
        assert output.ontology.instance_count() > 0


class TestThroughARun:
    def test_a_real_run_is_recorded_end_to_end(self, session, tmp_path):
        from phases.phase1_orchestrator import run_phase1

        source = tmp_path / "spec.txt"
        source.write_text("The orders endpoint is documented here.\n" * 40)

        run_phase1(
            workflow_id="wf-1",
            name="Orders API",
            document_paths=[str(source)],
            llm_client=Stub(),
            output_dir=str(tmp_path / "out"),
            db_session=session,
        )

        execution = session.query(PhaseExecution).filter_by(workflow_id="wf-1").first()
        assert execution.status == "completed"
        assert execution.tokens_used > 0

        steps = {e.details.get("step") for e in session.query(PhaseEvent).all()}
        assert {"load_documents", "index", "extract", "validate", "save"} <= steps

        assert session.query(Artifact).filter_by(artifact_type="ontology").count() == 1

    def test_a_failed_run_is_recorded_as_failed(self, session, tmp_path):
        """Otherwise a crashed run looks like one still in progress, forever."""
        from phases.phase1_orchestrator import run_phase1

        output = run_phase1(
            workflow_id="wf-bad",
            name="Orders API",
            document_paths=[str(tmp_path / "does-not-exist.txt")],
            llm_client=Stub(),
            output_dir=str(tmp_path / "out"),
            db_session=session,
        )

        assert output.status == "failed"
        execution = session.query(PhaseExecution).filter_by(workflow_id="wf-bad").first()
        assert execution.status == "failed"
        assert execution.error_message

    def test_review_flags_reach_the_audit_trail(self, session, tmp_path):
        """A run's own warning about itself must not live only in an HTTP response."""
        from phases.phase1_orchestrator import run_phase1

        source = tmp_path / "spec.txt"
        source.write_text("Unrelated filler text about weather.\n" * 400)

        run_phase1(
            workflow_id="wf-1",
            name="Orders API",
            document_paths=[str(source)],
            llm_client=Stub(),
            output_dir=str(tmp_path / "out"),
            db_session=session,
        )

        flags = session.query(PhaseEvent).filter_by(event_type="review_flag").all()
        assert flags
        assert all(f.severity == "warning" for f in flags)


class TestStatusSurvivesRestart:
    def test_status_can_be_read_back_from_the_database(self, session, tmp_path):
        from phases.phase1_orchestrator import run_phase1

        source = tmp_path / "spec.txt"
        source.write_text("The orders endpoint is documented here.\n" * 40)
        run_phase1(
            workflow_id="wf-1",
            name="Orders API",
            document_paths=[str(source)],
            llm_client=Stub(),
            output_dir=str(tmp_path / "out"),
            db_session=session,
        )

        recorded = run_status(session, "wf-1")

        assert recorded["status"] == "completed"
        assert recorded["phase"]["tokens_used"] > 0
        assert len(recorded["events"]) >= 5

    def test_an_unknown_workflow_reads_as_none(self, session):
        assert run_status(session, "never-existed") is None

    def test_no_session_reads_as_none(self):
        assert run_status(None, "wf-1") is None
