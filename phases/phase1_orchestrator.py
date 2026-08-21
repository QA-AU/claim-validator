"""Phase 1: Orchestrator - Document ingestion & domain-agnostic ontology extraction.

Two jobs, and since 2026-08-18 they are two modules:

    Phase 1a (here)   load → chunk and index → extract → save
    Phase 1b          validate → completeness → census → shape → type judge

Building and checking fail for different reasons, cost different amounts, and
are worth reporting separately — a check that fails leaves a perfectly good
ontology behind it, and the census is the only part of the phase whose cost
scales with the document rather than with retrieval. `run_phase1` still runs
both, so nothing changes for a caller; see `phases/phase1b_validation.py`.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import List

from phase1_model_config import token_rates
from phases.llm_usage import usage_of
from phases.phase1_models import Ontology, Phase1Output
from phases.phase1_document_loader import load_multiple_documents
from phases.phase1_rag_indexer import create_rag_index
from phases.phase1_generic_extractor import extract_ontology_generic
from phases.phase1b_validation import run_phase1b
from phases.phase1_storage import save_phase1_output, save_to_database
from phases.ontology_store import OntologyStore
from phases.run_tracker import RunTracker

logger = logging.getLogger(__name__)


def _seed_checklist_from_brief(store, ontology_key: str, brief) -> None:
    """Add the brief's known gaps as open checklist items, once.

    Deliberately left `kind=""` — unclassified. Retrieval decides whether each
    is really a document gap, exactly as for any other item. The brief says what
    a person believes is missing; only the document can confirm it.
    """
    from phases.gap_resolution import ChecklistItem

    gaps = brief.known_gaps()
    if not gaps:
        return

    items = store.load_checklist_items(ontology_key)
    existing = {i.question.strip().lower() for i in items}

    added = 0
    for gap in gaps:
        if gap.strip().lower() in existing:
            continue
        items.append(
            ChecklistItem(
                item_id=f"GAP-{len(items) + 1:03d}",
                question=gap,
                resolution_notes="Predicted by the brief; not yet checked against the document",
            )
        )
        added += 1

    if added:
        store.save_checklist_items(ontology_key, items)
        logger.info(f"[Phase 1] Seeded {added} checklist item(s) from the brief")


def run_phase1(
    workflow_id: str,
    name: str,
    document_paths: List[str],
    llm_client,
    output_dir: str = "./phase1_output",
    db_session=None,
    background_description: str = "",
    ontology_key: str = "",
    store: "OntologyStore | None" = None,
    promote: bool = True,
    tier: str = "",
    profile: str = "",
) -> Phase1Output:
    """Run Phase 1: Document Ingestion & Ontology Extraction.

    Args:
        workflow_id: Unique workflow ID
        name: Name for the ontology being built
        document_paths: Paths to the source documents
        llm_client: LLM client for extraction
        output_dir: Directory to save outputs
        db_session: Optional database session for logging
        background_description: What this material is, supplied by the user.
            Drives concept-type discovery, so the ontology suits the domain.
        ontology_key: Key of the ontology this run belongs to. When supplied
            with `store`, the run reuses that ontology's pinned concept schema
            and saves the result as a new version rather than a loose file.
        store: OntologyStore holding the ontology's versions and settings.
        promote: Whether this run's result becomes the ontology's current
            version. False stores it for review first — what a reviewed
            re-extraction needs, so the new version can be diffed and checked
            before anything downstream starts reading it.
        tier: Which model tier this run used, used only to look up configured
            token prices. Without it (or without configured prices) the run
            reports tokens but no cost.

    Returns:
        Phase1Output with extracted ontology and metadata
    """
    start_time = datetime.now()
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Records real step events when a session is supplied, so the UI can poll
    # actual progress instead of animating a guess. A no-op without one.
    tracker = RunTracker(db_session, workflow_id, name)
    tracker.start()

    try:
        # Step 1: Load documents
        logger.info(f"[Phase 1a] Step 1/4: Loading {len(document_paths)} documents...")
        tracker.step_start("load_documents", document_count=len(document_paths))
        documents = load_multiple_documents(document_paths, llm_client=llm_client)
        logger.info(f"✓ Loaded {len(documents)} documents")
        tracker.step_complete("load_documents", loaded=len(documents))

        # Step 2: Index for retrieval
        logger.info("[Phase 1a] Step 2/4: Creating RAG index...")
        tracker.step_start("index")
        rag_index = create_rag_index(documents)
        logger.info(f"✓ Created RAG index with {len(rag_index.chunks)} chunks")
        tracker.step_complete("index", chunks=len(rag_index.chunks))

        # Step 3: Extract the ontology
        logger.info("[Phase 1a] Step 3/4: Extracting ontology...")
        pinned = []
        if store and ontology_key:
            meta = store.load_meta(ontology_key)
            pinned = meta.pinned_concept_types if meta else []

        # A brief is guidance about what to look for, stored on the ontology so
        # every re-extraction reuses it rather than depending on whoever started
        # the run remembering to attach it again.
        brief = store.load_brief(ontology_key) if (store and ontology_key) else None
        if brief is not None and brief.is_empty:
            brief = None

        tracker.step_start("extract", pinned_schema=bool(pinned), brief=brief is not None)
        # Resolved before extraction rather than after: the shape rules are what
        # the run will be judged against, so the extractor is told them up front
        # instead of being marked down afterwards for not knowing.
        from phases.profiles import get_profile

        profile_key = profile
        if not profile_key and store and ontology_key:
            meta_for_profile = store.load_meta(ontology_key)
            if meta_for_profile:
                profile_key = meta_for_profile.profile
        active_profile = get_profile(profile_key)

        ontology = extract_ontology_generic(
            documents,
            rag_index,
            llm_client,
            name,
            background_description=background_description,
            pinned_concept_types=pinned,
            brief=brief,
            profile=active_profile,
        )
        logger.info(f"✓ Extracted ontology '{name}'")
        tracker.step_complete(
            "extract",
            concept_types=len(ontology.concept_types),
            instances=ontology.instance_count(),
        )

        # ---------------------------------------------------------- Phase 1b
        # Building the ontology is finished. Everything from here to the save
        # is *checking* it — a different job, with different failure modes and
        # opposite cost behaviour, so it lives in its own module. See
        # phases/phase1b_validation.py.
        logger.info("[Phase 1] Phase 1b: Checking the ontology...")
        checks = run_phase1b(
            ontology,
            rag_index,
            documents,
            llm_client,
            tracker,
            db_session=db_session,
            store=store,
            ontology_key=ontology_key,
            profile=active_profile,
        )
        validation = checks.validation
        output_completeness = checks.completeness
        census = checks.census
        shape_summary = checks.shape_check
        type_judge = checks.type_judge

        # Step 4: Save outputs — Phase 1a again; 1b never writes
        logger.info("[Phase 1a] Step 4/4: Saving outputs...")
        tracker.step_start("save")

        duration_seconds = (datetime.now() - start_time).total_seconds()

        # Read once, at the end: the client accumulated it across every call,
        # since `generate(prompt) -> str` has nowhere to return usage.
        usage = usage_of(llm_client)
        cost_cents = usage.cost_cents(token_rates(tier)) if tier else None

        output = Phase1Output(
            workflow_id=workflow_id,
            name=name,
            status="success" if validation.valid else "partial",
            ontology=ontology,
            validation=validation,
            documents_processed=len(documents),
            total_tokens_used=usage.total_tokens,
            # None means "no prices configured", which is not the same as free.
            total_cost_cents=cost_cents,
            usage=usage.to_dict(token_rates(tier) if tier else None),
            completeness=output_completeness,
            census=census,
            type_judge=type_judge,
            shape_check=shape_summary,
            duration_seconds=duration_seconds,
        )

        if usage.calls and not usage.is_complete:
            logger.warning(
                f"[Phase 1] {usage.uncounted_calls} of {usage.calls} calls reported no "
                f"token usage; {usage.total_tokens} is a floor, not a total"
            )

        save_phase1_output(output, str(output_path))
        logger.info(f"✓ Saved outputs to {output_path}")

        if store and ontology_key:
            version_path = store.save_version(
                ontology_key, ontology.to_dict(), workflow_id, make_current=promote
            )
            output.version_file = version_path.name
            # Persist the chunk stream this ontology was built from. Gap
            # resolution retrieves against it after the run, to separate "the
            # extraction missed it" from "the document doesn't say it" — and by
            # then the uploaded files are gone.
            store.save_index(ontology_key, rag_index.chunks, rag_index.metadata)
            # Saving the index discards any census bought against the old chunk
            # stream, because its chunk numbers no longer point anywhere real.
            # Correct, and it used to happen in silence — the run then reported
            # completeness as unestablished with nothing saying it had been
            # established before this run renumbered everything.
            dropped = getattr(store, "dropped_censuses", None) or []
            if dropped and not census.get("ran"):
                validation.review_flags.append(
                    f"{len(dropped)} saved census(es) were discarded because this run "
                    f"rebuilt the chunk stream ({', '.join(dropped)}). Completeness was "
                    f"measured before and is not measured now — re-run a census if you "
                    f"need the denominator"
                )
            # Pin the schema on the first successful run so later runs reuse it.
            if not pinned and ontology.concept_types:
                store.pin_schema(
                    ontology_key, [ct.to_dict() for ct in ontology.concept_types]
                )

            # Gaps the brief predicted become open checklist items. They are
            # still classified against the document before anyone is asked —
            # the brief is a person's belief about the document, not a finding.
            if brief is not None:
                _seed_checklist_from_brief(store, ontology_key, brief)

        tracker.step_complete("save", version_file=output.version_file)
        save_to_database(output, db_session, workflow_id, tracker=tracker)

        logger.info(f"[Phase 1] ✓ COMPLETED in {duration_seconds:.1f} seconds")
        logger.info(f"[Phase 1] Result: {output.status}")
        for concept_type in ontology.concept_types:
            logger.info(
                f"[Phase 1]   {concept_type.name}: {len(concept_type.instances)} instances"
            )
        logger.info(f"[Phase 1] Relations: {len(ontology.relations)}")
        logger.info(f"[Phase 1] Critical Areas: {ontology.critical_areas}")

        return output

    except Exception as e:
        duration_seconds = (datetime.now() - start_time).total_seconds()
        logger.error(f"[Phase 1] ✗ FAILED: {str(e)}", exc_info=True)

        # A failure has to reach the database too — otherwise a crashed run is
        # indistinguishable from one still in progress, forever.
        tracker.error("run", str(e))
        tracker.finish(
            status="failed",
            tokens_used=usage_of(llm_client).total_tokens,
            error_message=str(e),
        )

        return Phase1Output(
            workflow_id=workflow_id,
            name=name,
            status="failed",
            ontology=Ontology(name=name, domain=background_description or ""),
            validation=None,
            error_message=str(e),
            duration_seconds=duration_seconds,
        )
