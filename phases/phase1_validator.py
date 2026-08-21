"""Phase 1: Validator - Validate extracted ontology."""

import logging
from typing import List, Tuple

from phases.phase1_models import LOW_CHUNK_REACH, Ontology, ValidationResult
from phases.phase1_rag_indexer import LOW_TERM_OVERLAP

logger = logging.getLogger(__name__)

# Quality thresholds
CONFIDENCE_THRESHOLD = 0.7
MIN_CONCEPT_TYPES = 1
MIN_TOTAL_INSTANCES = 1

# Coverage gates *review*, never validity — a 0.4% ontology of a 12.9 MB document
# may be exactly what was wanted. It must simply never be mistaken for a complete
# one.
#
# `LOW_CHUNK_REACH` is defined on the Coverage model so the page and the API read
# one verdict rather than three copies of a number.
#
# Reach is structurally bounded by (retrieval calls x top_k) / chunks_total —
# roughly 75 chunks whatever the document size — so on anything large it is low
# by construction. The threshold therefore has to mean "far below what retrieval
# could have reached", not "less than complete", or every big document flags.
#
# Calibrated by measurement, not by intuition (2026-08-14, stub client):
#   RFC 6749, 182 chunks     — 19.2% reach, extraction genuinely good  -> must not flag
#   GitHub spec, 14,356      —  0.3% reach, the run that reported 1.0  -> must flag
# 5% sits with roughly 4x margin either side. A first guess of 25% flagged the
# RFC, which is exactly the false positive that trains people to ignore the flag.
THIN_CONCEPT_YIELD = 2.0

# Below this share of instances carrying a verified chunk citation, provenance is
# not dependable enough to answer "says who?" — set low because a citation is
# dropped whenever it cannot be verified, so some loss is expected and healthy.
LOW_CITATION_RATE = 0.5


def validate_ontology(ontology: Ontology, strict: bool = False) -> ValidationResult:
    """Validate an ontology.

    Deliberately domain-neutral: it checks that concepts were discovered and
    populated, not that any particular concept (endpoints, auth, ...) exists,
    since which concepts are appropriate depends on the material.
    """
    logger.info(f"Validating generic ontology '{ontology.name}'")

    issues: List[Tuple[str, str]] = []
    warnings: List[str] = []

    if len(ontology.concept_types) < MIN_CONCEPT_TYPES:
        issues.append(("critical", "No concept types were discovered"))

    total_instances = ontology.instance_count()
    if total_instances < MIN_TOTAL_INSTANCES:
        issues.append(("critical", "No concept instances were extracted"))

    # An unpopulated concept usually means retrieval missed it - worth surfacing
    # because the ontology looks complete while being silently narrower.
    empty = [ct.name for ct in ontology.concept_types if not ct.instances]
    if empty:
        warnings.append(f"Concept types with no instances found: {', '.join(empty)}")

    for concept_type in ontology.concept_types:
        if not concept_type.surface_terms:
            warnings.append(f"Concept '{concept_type.name}' has no surface terms to search with")
        for instance in concept_type.instances:
            if not instance.name:
                issues.append(("critical", f"Unnamed instance in concept '{concept_type.name}'"))

    if not ontology.constraints:
        warnings.append("No constraints extracted")
    if not ontology.critical_areas:
        warnings.append("No critical areas identified")
    if not ontology.relations:
        warnings.append("No relations between concepts identified")
    if not ontology.domain:
        warnings.append("No background description was supplied; extraction had no domain context")

    confidence = calculate_confidence(
        num_concept_types=len(ontology.concept_types),
        num_instances=total_instances,
        num_empty_concepts=len(empty),
        num_critical_issues=len([s for s, _ in issues if s == "critical"]),
        num_warnings=len(warnings),
    )

    valid = len([s for s, _ in issues if s == "critical"]) == 0
    if strict and warnings:
        valid = False

    ontology.confidence_score = confidence

    result = ValidationResult(
        valid=valid,
        issues=issues,
        warnings=warnings,
        confidence_score=confidence,
        coverage=ontology.coverage_report(),
        review_flags=coverage_review_flags(ontology),
    )
    logger.info(
        f"Validation: valid={valid}, confidence={confidence:.2f}, "
        f"{len(issues)} issues, {len(warnings)} warnings, "
        f"{len(result.review_flags)} review flags"
    )
    return result


def coverage_review_flags(ontology: Ontology) -> List[str]:
    """Reasons this result should not be read as a complete picture of the source.

    Deliberately separate from `warnings`, which feed the confidence score. These
    do not: reach and structure answer different questions, and a well-formed
    ontology of one percent of a document scores — correctly — 1.0 on structure.
    Folding coverage into that number would hide the one failure mode that
    matters here instead of exposing it.
    """
    flags: List[str] = []
    coverage = ontology.coverage

    # An empty extraction is the loudest possible failure and was the quietest.
    # Observed twice live: concept discovery returned nothing, the run reported
    # `status: success`, structure scored 1.0 because a well-formed empty
    # ontology is well-formed, citation rate was 1.0 because zero of zero
    # instances are cited, and the whole thing read as a clean run in the
    # report. Nothing downstream distinguished it from a sparse one.
    if not ontology.concept_types:
        flags.append(
            "No concept types were discovered — this ontology is EMPTY and describes "
            "nothing. Extraction is non-deterministic; re-run before drawing any "
            "conclusion from it"
        )
    elif not any(ct.instances for ct in ontology.concept_types):
        flags.append(
            f"{len(ontology.concept_types)} concept type(s) were discovered but not one "
            f"instance was extracted — this ontology is empty of content. Re-run, and "
            f"if it recurs the concepts do not match the document's wording"
        )

    if coverage.reach_is_low:
        flags.append(
            f"Extraction consulted {len(coverage.chunks_consulted)} of "
            f"{coverage.chunks_total} chunks ({coverage.chunk_reach:.1%}) — this "
            f"ontology describes a sample of the document, not all of it"
        )

    if ontology.concept_types and ontology.concept_yield() < THIN_CONCEPT_YIELD:
        flags.append(
            f"{ontology.concept_yield():.1f} instances per concept type on average "
            f"— thin for {len(ontology.concept_types)} concepts"
        )

    # Retrieval is lexical: a probe sharing no terms with the document scores
    # exactly 0.0, which is mathematically identical to no relationship at all.
    # The concept still gets populated, from text picked arbitrarily.
    unmatched = [
        ct.name
        for ct in ontology.concept_types
        if ct.instances and ct.retrieval_score <= 0.0 and ct.chunks_consulted
    ]
    if unmatched:
        flags.append(
            f"Retrieval matched nothing for {', '.join(unmatched)} — these concepts were "
            f"populated from arbitrarily selected text, not from a lexical match. "
            f"Likely a document that paraphrases rather than reuses the probe wording"
        )

    # Matched something, but on too little of the probe to trust. This is the
    # case the grounding flag above cannot catch: a non-zero score carried by an
    # incidental shared word, returning a passage about something else.
    weak = [
        ct.name
        for ct in ontology.concept_types
        if ct.instances and ct.retrieval_score > 0 and ct.term_overlap < LOW_TERM_OVERLAP
    ]
    if weak:
        flags.append(
            f"Retrieval for {', '.join(weak)} matched on only a small part of the probe — "
            f"the passages used may be about something else that happens to share a word"
        )

    # Provenance depends on the model citing its source. If it stops, the
    # ontology looks unchanged while nothing in it can be traced any more.
    if ontology.instance_count() and ontology.citation_rate() < LOW_CITATION_RATE:
        flags.append(
            f"Only {ontology.cited_count()} of {ontology.instance_count()} instances "
            f"cite the passage they came from ({ontology.citation_rate():.0%}) — the "
            f"rest cannot be traced back to the document"
        )

    # Any image that produced no caption is document content that never reached
    # the text, and so could not be retrieved or extracted.
    if coverage.images_found and coverage.images_captioned < coverage.images_found:
        lost = coverage.images_found - coverage.images_captioned
        flags.append(
            f"{lost} of {coverage.images_found} images produced no caption — "
            f"their content is absent from the ontology"
        )

    return flags


def calculate_confidence(
    num_concept_types: int,
    num_instances: int,
    num_empty_concepts: int,
    num_critical_issues: int,
    num_warnings: int,
) -> float:
    """Confidence for a generic ontology (0.0-1.0)."""
    if num_concept_types == 0 or num_instances == 0:
        return 0.0

    score = 1.0

    # A single concept type is a thin ontology.
    if num_concept_types < 2:
        score -= 0.2

    # Sparse population suggests retrieval or extraction underperformed.
    if num_instances < num_concept_types * 2:
        score -= 0.1

    if num_concept_types:
        score -= 0.2 * (num_empty_concepts / num_concept_types)

    score -= num_critical_issues * 0.3
    score -= num_warnings * 0.05

    return max(0.0, min(1.0, score))


def summarize_validation(validation: ValidationResult) -> str:
    """Generate human-readable validation summary."""
    lines = []

    if validation.valid:
        lines.append("✓ Ontology validation PASSED")
    else:
        lines.append("✗ Ontology validation FAILED")

    # "Structure Score", not "Quality" — it says the ontology is well-formed, not
    # that it captured the document. Coverage below answers that second question.
    lines.append(f"Structure Score: {validation.confidence_score:.2f}/1.0")

    coverage = validation.coverage
    if coverage:
        lines.append(
            f"Coverage: {coverage.get('chunks_consulted_count', 0)}/"
            f"{coverage.get('chunks_total', 0)} chunks consulted "
            f"({coverage.get('chunk_reach', 0.0):.1%}), "
            f"{coverage.get('concept_yield', 0.0)} instances per concept"
        )
        if coverage.get("images_found"):
            lines.append(
                f"Images: {coverage.get('images_captioned', 0)}/"
                f"{coverage['images_found']} captioned"
            )

    if validation.review_flags:
        lines.append("\nFlagged for review:")
        for flag in validation.review_flags:
            lines.append(f"  ⚑ {flag}")

    if validation.issues:
        lines.append("\nCritical Issues:")
        for severity, message in validation.issues:
            if severity == "critical":
                lines.append(f"  ✗ {message}")

    if validation.warnings:
        lines.append("\nWarnings:")
        for warning in validation.warnings[:5]:  # Show first 5
            lines.append(f"  ⚠ {warning}")

        if len(validation.warnings) > 5:
            lines.append(f"  ... and {len(validation.warnings) - 5} more warnings")

    return "\n".join(lines)
