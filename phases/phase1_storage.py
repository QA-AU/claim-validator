"""Phase 1: Storage - Save ontology to files and database."""

import json
import re
import logging
from pathlib import Path

from phases.phase1_models import Ontology, Phase1Output, ValidationResult
from phases.phase2_adapter import to_phase2_dict

logger = logging.getLogger(__name__)


def save_ontology_json(ontology: Ontology, output_path: str, workflow_id: str = "") -> str:
    """Save the ontology as JSON.

    Written in the Phase 2 projection, since that is the contract downstream
    phases read. `Ontology.to_dict()` is the pure generic form.
    """
    output_path = Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    # The workflow id is in the filename as well as the directory: the directory
    # gives uniqueness, but the file has to identify itself once it is copied
    # out of its folder — into a ticket, an email, or Phase 2 by path.
    slug = re.sub(r"[^a-z0-9]+", "-", ontology.name.lower()).strip("-") or "ontology"
    file_name = f"ontology_{slug}_{workflow_id}.json" if workflow_id else f"ontology_{slug}.json"
    file_path = output_path / file_name

    with open(file_path, "w") as f:
        json.dump(to_phase2_dict(ontology), f, indent=2)

    logger.info(f"Saved ontology to: {file_path}")
    return str(file_path)


def summarise_ontology(ontology: Ontology) -> dict:
    """Summary of the ontology for the run report."""
    return {
        "num_concept_types": len(ontology.concept_types),
        "num_instances": ontology.instance_count(),
        "concept_types": {ct.name: len(ct.instances) for ct in ontology.concept_types},
        "num_relations": len(ontology.relations),
        "num_constraints": len(ontology.constraints),
        "critical_areas": ontology.critical_areas,
    }


def save_phase1_output(output: Phase1Output, output_path: str) -> None:
    """Save complete Phase 1 output including ontology, validation, and metadata."""
    output_path = Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    # Save ontology JSON
    ontology_file = save_ontology_json(output.ontology, str(output_path), output.workflow_id)
    output.ontology_file = ontology_file

    # Save report
    report = {
        "workflow_id": output.workflow_id,
        "name": output.name,
        "status": output.status,
        "documents_processed": output.documents_processed,
        "total_tokens_used": output.total_tokens_used,
        "total_cost_cents": output.total_cost_cents,
        "usage": output.usage,
        "completeness": output.completeness,
        # The orchestrator populates this and the report used to drop it, so a
        # saved report could not say whether the shape check had run — the one
        # check that is unconditional and free was the one missing from the
        # record of what was done.
        "shape_check": output.shape_check,
        "census": output.census,
        "type_judge": output.type_judge,
        "duration_seconds": output.duration_seconds,
        "ontology_file": ontology_file,
        "validation": {
            "valid": output.validation.valid,
            "confidence_score": output.validation.confidence_score,
            "num_critical_issues": len([s for s, _ in output.validation.issues if s == "critical"]),
            "num_warnings": len(output.validation.warnings),
            # Reach, kept distinct from the structure score above. Recorded in the
            # report because it cannot be recovered from the saved ontology later.
            "coverage": output.validation.coverage,
            "review_flags": output.validation.review_flags,
        },
        "ontology_summary": summarise_ontology(output.ontology),
    }

    # Same stem as the workbook, so a run's JSON and Excel reports sit together
    # under one name rather than differing by a redundant prefix.
    from phases.run_report_stem import report_stem

    stem = report_stem("phase1", output.workflow_id)
    report_file = output_path / f"{stem}_report.json"
    with open(report_file, "w") as f:
        json.dump(report, f, indent=2)

    logger.info(f"Saved report to: {report_file}")

    # Save validation details
    if output.validation.issues or output.validation.warnings or output.validation.review_flags:
        validation_file = output_path / f"{stem}_validation.json"
        validation_data = {
            "workflow_id": output.workflow_id,
            "name": output.name,
            "valid": output.validation.valid,
            "confidence_score": output.validation.confidence_score,
            "critical_issues": [
                {"severity": severity, "message": msg}
                for severity, msg in output.validation.issues
                if severity == "critical"
            ],
            "warnings": output.validation.warnings,
            "review_flags": output.validation.review_flags,
            "coverage": output.validation.coverage,
        }

        with open(validation_file, "w") as f:
            json.dump(validation_data, f, indent=2)

        logger.info(f"Saved validation details to: {validation_file}")


def save_to_database(
    output: Phase1Output,
    db_session,
    workflow_id: str,
    tracker=None,
) -> None:
    """Save Phase 1 results to the database.

    Until 2026-08-14 this imported `ExecutionStage` and `WorkflowExecution`,
    neither of which has ever existed in `db/models.py`. The `except ImportError`
    below turned that into a warning, so the function appeared to work and wrote
    nothing. Nothing caught it because the web app passed `db_session=None`, so
    it was never called at all.

    It now writes through `RunTracker` against the real schema: `PhaseExecution`
    for status and cost, `Artifact` for the versioned ontology.
    """
    if db_session is None:
        return

    try:
        from phases.run_tracker import RunTracker

        tracker = tracker or RunTracker(db_session, workflow_id, output.name)
        tracker.finish(
            status="completed" if output.status == "success" else "failed",
            tokens_used=output.total_tokens_used,
            cost_cents=output.total_cost_cents,
            error_message=output.error_message,
        )

        version = tracker.save_artifact(
            "ontology",
            {
                "ontology": output.ontology.to_dict(),
                "summary": summarise_ontology(output.ontology),
                "ontology_file": output.ontology_file,
                "version_file": output.version_file,
                "duration_seconds": output.duration_seconds,
                "usage": output.usage,
                "completeness": output.completeness,
                # Per-instance findings, not only their counts. Both objects
                # carried this already; it just never reached the artifact a
                # report reads back — the same gap Phase 3's judge had.
                "shape_check": output.shape_check,
                "type_judge": output.type_judge,
            },
        )

        output.execution_stage_id = tracker.phase_execution_id
        logger.info(
            f"Saved run {workflow_id} to database"
            + (f" as ontology artifact v{version}" if version else "")
        )

    except Exception as e:
        # A failed audit write must not lose a completed extraction.
        logger.error(f"Error saving to database: {e}", exc_info=True)


def load_ontology_json(file_path: str) -> dict:
    """Load ontology from JSON file."""
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"Ontology file not found: {file_path}")

    with open(file_path, "r") as f:
        ontology_dict = json.load(f)

    logger.info(f"Loaded ontology from: {file_path}")
    return ontology_dict
