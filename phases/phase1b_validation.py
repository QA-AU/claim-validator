"""Phase 1b: checking the ontology Phase 1a built.

Phase 1 did two unrelated jobs under one name. **1a builds**: load the
documents, chunk and index them, ask a model what is in there. **1b checks**:
is the result well formed, how much of the document did it see, how much of
what is there did it capture, and is each instance really the kind of thing it
was filed as. Splitting them says three things the single step could not.

### They fail for different reasons and cost different amounts

1a fails when a document will not load or a model will not answer. 1b fails
when a *check* fails, which leaves a perfectly good ontology behind it — so
every function here reports "not checked" rather than raising, and none of them
can lose an extraction that already succeeded.

Cost is the sharper difference. 1a's cost is bounded by retrieval: a 12.9 MB
specification costs about the same as a 2.5 KB one. 1b's census is the only
part of Phase 1 whose cost scales with the *document*, which is why it is gated
on size and why it announces what it would have cost when it declines. Those
are opposite economics and they were being reported as one number.

### What runs here

    validate       structure score, chunk reach, citation rate — free
    completeness   exact counts where the document parses — free
    census         reads every chunk and counts what is there — LLM, gated
    shape check    is each instance usable: named, cited, distinct — free
    type judge     is each instance the KIND of thing it was filed as — LLM

Four of the five are free and deterministic and run unconditionally. That is
deliberate and it has been got wrong before: the shape check was once gated on
an ontology store being in use, so a plain command-line extraction received no
checking at all.

### It is a stage, not a separate command

`run_phase1` calls this between extracting and saving, so one command still
does the whole job. The separation is in the code and in the record, not in
what a user has to run. Nothing here writes the ontology or the version file —
that is Phase 1a's save step, and a checker that could modify what it checks
would be a different and much worse thing.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from phases.phase1_validator import summarize_validation, validate_ontology

logger = logging.getLogger(__name__)


@dataclass
class Phase1bResult:
    """Everything the checks established, and everything they could not.

    `validation` carries the structure score and the review flags, and is the
    same object `Phase1Output` stores — the flags raised here are appended to
    it, so a caller reading only the validation still sees them.
    """

    validation: Any = None
    completeness: Dict[str, Any] = field(default_factory=dict)
    census: Dict[str, Any] = field(default_factory=dict)
    shape_check: Dict[str, Any] = field(default_factory=dict)
    type_judge: Dict[str, Any] = field(default_factory=dict)

    @property
    def review_flags(self) -> List[str]:
        return list(getattr(self.validation, "review_flags", []) or [])

    @property
    def completeness_established(self) -> bool:
        """Whether anything here actually measured how much was captured.

        Chunk reach does not: it is a ceiling. Only a census that ran and read
        the whole document establishes this, which is why it is asked as its own
        question rather than inferred from a score.
        """
        return bool(self.census.get("ran"))


def run_type_judge(ontology, rag_index, llm_client, tracker, db_session):
    """Ask whether each sampled instance is really the kind of thing it was
    filed as, and report the majority verdict over several runs.

    Never raises: this is a check on a result that already exists, and losing
    the extraction because the check failed would be the wrong trade. A failure
    is reported as "not checked", never as "checked and clean".
    """
    from phases.settings_registry import settings_for
    from phases.type_check import (
        DEFAULT_SETTINGS as TC_SETTINGS,
        JUDGE_SAMPLE,
        SETTINGS_PROCESS as TC_PROCESS,
        judge_types_repeated,
    )

    resolved = settings_for(TC_PROCESS, TC_SETTINGS, None, db_session)
    if not resolved.get("run_judge"):
        return {"ran": False, "why": "type_check.run_judge is off"}
    if llm_client is None or not rag_index.chunks:
        return {"ran": False, "why": "no model client or no chunks to judge against"}

    runs = int(resolved.get("judge_runs") or 3)
    sample = int(resolved.get("judge_sample") or JUDGE_SAMPLE)
    tracker.step_start("type_judge", sample=sample, runs=runs)
    try:
        report = judge_types_repeated(
            ontology, rag_index.chunks, llm_client, sample_size=sample, runs=runs
        )
    except Exception as e:
        logger.error(f"[Phase 1b] Type judge failed: {e}", exc_info=True)
        tracker.error("type_judge", str(e))
        return {"ran": False, "why": f"type judge failed: {e}"}

    tracker.step_complete(
        "type_judge",
        judged=report.judged,
        runs=runs,
        wrong_kind=len(report.unsupported),
        weak_citation=len(report.weakly_cited),
        skipped_uncited=report.skipped_uncited,
    )
    return {
        "ran": True,
        "runs": runs,
        "judged": report.judged,
        "wrong_kind": len(report.unsupported),
        "weak_citation": len(report.weakly_cited),
        "review_flags": report.review_flags(),
        "verdicts": [
            {"instance_id": v.instance_id, "concept": v.concept, "name": v.name,
             "verdict": v.verdict, "reason": v.reason,
             "agreement": f"{v.agreement}/{v.runs_judged}"}
            for v in report.verdicts if v.verdict != "ok"
        ],
    }


def run_census(ontology, rag_index, llm_client, tracker, settings):
    """Measure completeness directly, as a range, or say why it was not measured.

    A census reads the whole document, so it is gated on size and reported with
    its spread. Never a single count: two identical census runs on 182 chunks of
    prose returned 294 and 342 instances, and reporting either one of those as
    the denominator is how a measurement with an error bar becomes a fact.
    """
    from phases.census import census_repeated

    chunks = rag_index.chunks
    concepts = [(c.name, c.description or "") for c in ontology.concept_types]
    limit = int(settings.get("census_max_chunks") or 0)

    if not settings.get("census_on_completion"):
        return {"ran": False, "why": "census_on_completion is off"}
    if not concepts or not chunks:
        return {"ran": False, "why": "nothing to census"}
    if len(chunks) > limit:
        # Refused rather than skipped: the difference is that a refusal says
        # what it would have cost and how to ask for it.
        from phases.census import CENSUS_BATCH, estimate_calls

        runs = int(settings.get("census_runs") or 3)
        per_run = estimate_calls(len(chunks), CENSUS_BATCH)
        return {
            "ran": False,
            "why": (f"{len(chunks)} chunks is over the {limit}-chunk limit for an "
                    f"automatic census"),
            "would_cost": f"about {per_run * runs} call(s) for {runs} repeat(s)",
            "how": "python -m phases.cli_targeted --ontology-file <ontology> "
                   "--sources <sources> --plan-only",
        }

    runs = int(settings.get("census_runs") or 3)
    tracker.step_start("census", chunks=len(chunks), concepts=len(concepts), runs=runs)
    try:
        spreads = census_repeated(concepts, chunks, llm_client, runs=runs)
    except Exception as e:
        # Completeness is the optional half of a run. Losing it must not lose
        # the ontology that has already been built.
        logger.error(f"[Phase 1b] Census failed: {e}", exc_info=True)
        tracker.error("census", str(e))
        return {"ran": False, "why": f"census failed: {e}"}

    # Capture is measured by NAME across every concept, not by count within one.
    # The one-pass census files an instance under whichever concept it judges,
    # and where the ontology holds two concept types describing the same things
    # it will not always choose the one extraction chose. Measured: 22 of
    # `api_operation`'s instances read as missing when the census had found all
    # 22 under `endpoint`. Comparing counts per concept measures whether two
    # passes picked the same word.
    from phases.name_reconciliation import reconcile_across_concepts

    extracted_by_concept = {
        c.name: [i.name for i in c.instances if i.name] for c in ontology.concept_types
    }
    # A census with failed batches read only part of the document, so its counts
    # are a floor with no ceiling and its range is meaningless. Found live: 17
    # of 19 batches failed on an exhausted API key and the run reported
    # "16-162 instance(s) exist" as though it were a measurement. An incomplete
    # census is not a denominator, and calling it one is the exact failure this
    # project exists to catch.
    incomplete = [name for name, spread in spreads.items() if not spread.complete]
    if incomplete:
        logger.warning(
            f"[Phase 1b] Census incomplete for {len(incomplete)} concept(s); reporting "
            f"it as not measured rather than as a range"
        )
        tracker.step_complete(
            "census", runs=runs, concepts=len(concepts), incomplete=len(incomplete),
        )
        return {
            "ran": False,
            "why": (f"the census could not read the whole document — {len(incomplete)} "
                    f"concept(s) had failed batches. Its counts are a floor with no "
                    f"ceiling, so they are not reported as a range"),
            "incomplete": True,
            "concepts_incomplete": incomplete,
        }

    sampled = {c.name: len(c.instances) for c in ontology.concept_types}

    try:
        crossed = reconcile_across_concepts(
            extracted_by_concept,
            {name: spread.probable for name, spread in spreads.items()},
            llm_client=llm_client,
        )
    except Exception as e:
        # Reconciliation is how the number is made honest, not how it is
        # produced. Losing it must not lose the census that has been paid for.
        logger.warning(f"[Phase 1b] Cross-concept reconciliation failed ({e}); "
                       f"capture is reported by count, which overstates gaps")
        crossed = {}

    per_concept = {}
    for name, spread in spreads.items():
        entry = spread.to_dict()
        entry["extracted"] = sampled.get(name, 0)

        match = crossed.get(name)
        if match is not None:
            found = len(match.matched)
            entry["matched"] = found
            entry["matched_elsewhere"] = len(match.elsewhere)
            entry["filed_elsewhere"] = {n: c for n, (_, c) in match.elsewhere.items()}
            entry["missing"] = match.unmatched
            # A range, because the census itself is one: the low count is the
            # most favourable denominator and the high the least.
            entry["capture_range"] = (
                (round(min(found / spread.high, 1.0), 4),
                 round(min(found / spread.low, 1.0), 4))
                if spread.high else None
            )
            entry["capture_basis"] = "names matched across all concepts"
        else:
            entry["capture_range"] = spread.capture_range(sampled.get(name, 0))
            entry["capture_basis"] = "counts within the concept — reconciliation unavailable"

        per_concept[name] = entry

    total_low = sum(s.low for s in spreads.values())
    total_high = sum(s.high for s in spreads.values())
    extracted = sum(sampled.values())
    # Concepts whose instances the census filed under a different label. Not an
    # error — usually the ontology holds two concept types for the same things,
    # which is worth knowing and is invisible without this comparison.
    disagreements = {
        name: entry["filed_elsewhere"]
        for name, entry in per_concept.items() if entry.get("filed_elsewhere")
    }

    tracker.step_complete(
        "census", runs=runs, concepts=len(concepts),
        census_low=total_low, census_high=total_high, extracted=extracted,
    )
    return {
        "ran": True,
        "runs": runs,
        "extracted": extracted,
        "census_low": total_low,
        "census_high": total_high,
        "per_concept": per_concept,
        "concept_disagreements": disagreements,
    }


def measure_completeness(ontology, rag_index, documents, store, ontology_key):
    """Completeness for this run, or None if it could not be measured.

    Uses the documents' original text rather than the chunk stream: chunks
    overlap by 100 characters, so rejoining them yields something larger than
    the source that no longer parses as JSON. Exact counts need the real thing.
    """
    try:
        from phases.completeness import measure
        from phases.profiles import get_profile

        profile = None
        if store and ontology_key:
            meta = store.load_meta(ontology_key)
            if meta:
                profile = get_profile(meta.profile)

        source_text = "\n\n".join(d.raw_text or "" for d in documents)
        return measure(
            ontology, chunks=rag_index.chunks, source_text=source_text, profile=profile
        )
    except Exception as e:
        # A measurement failure must not lose a completed extraction.
        logger.error(f"[Phase 1b] Could not measure completeness: {e}", exc_info=True)
        return None


def run_phase1b(
    ontology,
    rag_index,
    documents,
    llm_client,
    tracker,
    db_session=None,
    store=None,
    ontology_key: str = "",
    profile=None,
) -> Phase1bResult:
    """Check an ontology, and report what was and was not established.

    Returns rather than raises for every check. The ontology exists by the time
    this runs; a failed check is a gap in what is known about it, not a reason to
    throw it away.
    """
    result = Phase1bResult()

    tracker.step_start("validate")
    validation = validate_ontology(ontology)
    result.validation = validation
    logger.info(f"✓ Validation complete (confidence: {validation.confidence_score:.2f})")
    logger.info(f"\n{summarize_validation(validation)}")
    tracker.step_complete(
        "validate",
        structure_score=validation.confidence_score,
        chunk_reach=validation.coverage.get("chunk_reach"),
        # Recorded because the run report scores traceability from it, and a
        # figure the report needs but the trail never captured would have to be
        # recomputed from an artifact that may since have moved.
        citation_rate=validation.coverage.get("citation_rate"),
        instances_total=validation.coverage.get("instances_total"),
        review_flags=len(validation.review_flags),
    )

    # Completeness needs no model either: exact counts when the document parses,
    # and per-concept unread material otherwise. Chunk reach says how much was
    # *touched*; this says how much was *captured*.
    tracker.step_start("completeness")
    completeness = measure_completeness(ontology, rag_index, documents, store, ontology_key)
    if completeness is not None:
        validation.review_flags.extend(completeness.review_flags())
        result.completeness = completeness.to_dict()
        tracker.step_complete(
            "completeness",
            exact_totals=len(completeness.exact),
            needs_review=completeness.needs_review,
        )
    else:
        tracker.step_complete("completeness", exact_totals=0)

    # Completeness measured directly, gated on document size and reported as a
    # range. The step above estimates from what was read; this goes and counts.
    # It is the only check here that costs model calls in proportion to the
    # document, which is why it is gated and why it announces what it would have
    # cost when it declines.
    from phases.phase1_generic_extractor import (
        DEFAULT_SETTINGS as P1_SETTINGS,
        SETTINGS_PROCESS as P1_PROCESS,
    )
    from phases.settings_registry import settings_for

    census_settings = settings_for(P1_PROCESS, P1_SETTINGS, None, db_session)
    census = run_census(ontology, rag_index, llm_client, tracker, census_settings)
    result.census = census
    if census.get("ran"):
        validation.review_flags.append(
            f"Completeness measured: extraction has {census['extracted']} instance(s) "
            f"against a census of {census['census_low']}–{census['census_high']} over "
            f"{census['runs']} runs. The census is a range, not a count — two "
            f"identical runs on dense prose have differed by 16%"
        )
        if census.get("concept_disagreements"):
            pairs = ", ".join(
                f"{concept} (as {', '.join(sorted(set(where.values())))})"
                for concept, where in census["concept_disagreements"].items()
            )
            validation.review_flags.append(
                f"The census filed some instances under a different concept than "
                f"extraction did: {pairs}. They are counted as captured — they are "
                f"in the ontology — but two concept types describing the same "
                f"things is worth a look"
            )
    elif census.get("incomplete"):
        validation.review_flags.append(
            f"Completeness NOT measured — {census['why']}. Re-run when the "
            f"model is reachable; a partial census is not a denominator"
        )
    elif census.get("would_cost"):
        validation.review_flags.append(
            f"Completeness NOT measured — {census['why']}. It would cost "
            f"{census['would_cost']}. Until it is run, nothing here establishes "
            f"how much of the document was captured"
        )

    # Shape rules are deterministic and free, so they run on every extraction.
    # Unconditional. This was previously guarded by `if store and ontology_key`,
    # so a plain command-line extraction received no shape checking at all — a
    # free, deterministic check skipped for a reason that had nothing to do with
    # cost. The profile decides whether any rule applies; the check itself always
    # runs and always reports what it did.
    from phases.type_check import shape_report_for

    shapes = shape_report_for(ontology, profile, tracker=tracker)
    validation.review_flags.extend(shapes.review_flags())
    result.shape_check = shapes.to_dict()
    if not shapes.ran:
        logger.info(
            "[Phase 1b] Shape check ran with no applicable rules — nothing was "
            "checked, which is not the same as nothing being wrong"
        )

    # The one check that catches an instance filed as the wrong KIND of thing.
    # Nothing free can: a tag filed as an endpoint has a name, a citation and no
    # duplicate, so every deterministic check passes it — and that failure took
    # 19 of 21 extracted endpoints on the GitHub specification.
    type_judge = run_type_judge(ontology, rag_index, llm_client, tracker, db_session)
    result.type_judge = type_judge
    validation.review_flags.extend(type_judge.get("review_flags") or [])

    # Coverage flags are the run's own warning about itself; they belong in the
    # audit trail, not only in a response the browser may never see.
    for flag in validation.review_flags:
        tracker.event("review_flag", "validate", {"message": flag}, severity="warning")

    return result
