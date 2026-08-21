"""Ties retrieval, shape check, entailment judge and the gap report into one
call — used identically by the CLI script and the async job worker, so the
HTTP layer adds no business logic of its own, only request/response
marshalling.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from phases.entailment import judge_entailment
from phases.ontology_store import OntologyStore
from phases.phase1_models import Ontology
from phases.phase1_orchestrator import run_phase1
from phases.requirement_shapes import check_requirement_shapes
from phases.run_tracker import RunTracker

from claimvalidator.claim_retrieval import retrieve_for_claim
from claimvalidator.claim_shims import ResolvedClaim, _ClaimSet, _JudgeClaim, shape_profile
from claimvalidator.document_identity import resolve_ontology_key
from claimvalidator.gap_report import GapReport, build_gap_report


@dataclass
class ClaimResult:
    id: str
    text: str
    shape_ok: bool
    shape_reason: Optional[str]
    verdict: str
    judged: bool
    agreement: Optional[str]
    cited_chunks: List[int]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "shape": {"ok": self.shape_ok, "reason": self.shape_reason},
            "verdict": self.verdict,
            "judged": self.judged,
            "agreement": self.agreement,
            "cited_chunks": self.cited_chunks,
            "reason": self.reason,
        }


@dataclass
class ValidationResult:
    ontology_key: str
    ontology_reused: bool
    per_claim: List[ClaimResult] = field(default_factory=list)
    gap_report: Optional[GapReport] = None
    quality: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ontology_key": self.ontology_key,
            "ontology_reused": self.ontology_reused,
            "per_claim": [c.to_dict() for c in self.per_claim],
            "gap_report": self.gap_report.to_dict() if self.gap_report else None,
            "quality": self.quality,
        }


def run_validation(
    workflow_id: str,
    document_paths: List[str],
    claims_input: List[Dict[str, str]],
    llm_client,
    document_id: Optional[str] = None,
    background_description: str = "",
    store_root: str = "./.data/ontologies",
    output_dir: str = "./.data/phase1_output",
    db_session=None,
    shape_rule_overrides: Optional[Dict[str, Any]] = None,
    census_max_chunks: int = 200,
    force_census: bool = True,
    census_runs: int = 3,
) -> ValidationResult:
    tracker = RunTracker(db_session, workflow_id, name=document_id or "validation",
                          phase_name="claim_ontology")
    tracker.start()

    store = OntologyStore(root=store_root)
    tracker.step_start("resolve_ontology")
    ontology_key, reused = resolve_ontology_key(store, document_paths, document_id)
    tracker.step_complete("resolve_ontology", ontology_key=ontology_key, reused=reused)

    if not store.has_index(ontology_key):
        tracker.step_start("build_ontology")
        run_phase1(
            workflow_id=workflow_id,
            name=document_id or ontology_key,
            document_paths=document_paths,
            llm_client=llm_client,
            store=store,
            ontology_key=ontology_key,
            background_description=background_description,
            output_dir=output_dir,
            db_session=db_session,
        )
        tracker.step_complete("build_ontology")

    ontology = Ontology.from_dict(store.load_current(ontology_key))
    searcher = store.searcher_for(ontology_key)
    index = store.load_index(ontology_key)
    chunks: List[str] = index["chunks"]
    source_text = "\n\n".join(chunks)  # approximate — chunks overlap, fine for section-matching

    tracker.finish("success")

    claims = [ResolvedClaim(id=c["id"], text=c["text"]) for c in claims_input]

    retrieval_tracker = RunTracker(db_session, workflow_id, name=document_id or "validation",
                                    phase_name="claim_retrieval")
    retrieval_tracker.start()
    retrieval_tracker.step_start("retrieve")
    found_nothing = 0
    for claim in claims:
        result = retrieve_for_claim(claim.text, ontology, searcher, llm_client)
        claim.source_chunks = result.chunk_indices
        if not result.chunk_indices:
            found_nothing += 1
    retrieval_tracker.step_complete("retrieve", claims=len(claims), found_nothing=found_nothing)
    retrieval_tracker.finish("success")

    shape_tracker = RunTracker(db_session, workflow_id, name=document_id or "validation",
                                phase_name="claim_shape_check")
    shape_tracker.start()
    shape_report = check_requirement_shapes(
        _ClaimSet(claims), profile=shape_profile(shape_rule_overrides), tracker=shape_tracker,
    )
    violations_by_id = {v.item_id: v.reason for v in shape_report.violations}
    shape_tracker.finish("success")

    judge_tracker = RunTracker(db_session, workflow_id, name=document_id or "validation",
                                phase_name="claim_entailment")
    judge_tracker.start()
    entailment_report = judge_entailment(
        [_JudgeClaim(c) for c in claims], chunks, llm_client, db_session=db_session,
    )
    verdicts_by_id = {v.requirement_id: v for v in entailment_report.verdicts}
    judge_tracker.step_complete(
        "judge",
        judged=len(entailment_report.judged),
        entailed=len(entailment_report.entailed),
        mentions_only=len(entailment_report.mentions_only),
        contradicted=len(entailment_report.contradicted),
        no_evidence=len(entailment_report.no_evidence),
    )
    judge_tracker.finish("success")

    per_claim: List[ClaimResult] = []
    for claim in claims:
        verdict = verdicts_by_id.get(claim.id)
        per_claim.append(ClaimResult(
            id=claim.id,
            text=claim.text,
            shape_ok=claim.id not in violations_by_id,
            shape_reason=violations_by_id.get(claim.id),
            verdict=verdict.verdict if verdict else "unjudged",
            judged=bool(verdict and verdict.judged),
            agreement=f"{verdict.agreement}/{verdict.runs_judged}" if verdict else None,
            cited_chunks=claim.source_chunks,
            reason=(verdict.reason if verdict else "no citation found by retrieval"),
        ))

    completeness_tracker = RunTracker(db_session, workflow_id, name=document_id or "validation",
                                       phase_name="claim_completeness")
    completeness_tracker.start()
    gap = build_gap_report(
        ontology, chunks, llm_client, claims, source_text=source_text,
        runs=census_runs, max_chunks=census_max_chunks, force=force_census,
    )
    completeness_tracker.step_complete("gap_report", ran=gap.ran,
                                        concepts=len(gap.per_concept))
    completeness_tracker.finish("success" if gap.ran else "skipped")

    quality = {
        "claims_submitted": len(claims),
        "shape_checked": shape_report.checked,
        "shape_violations": len(shape_report.violations),
        "retrieval_found_nothing": found_nothing,
        "judged": len(entailment_report.judged),
        "entailed": len(entailment_report.entailed),
        "mentions_only": len(entailment_report.mentions_only),
        "contradicted": len(entailment_report.contradicted),
        "no_evidence": len(entailment_report.no_evidence),
        "runs": entailment_report.runs,
        "concepts_covered": sum(1 for g in gap.per_concept.values() if g.addressed_count > 0),
        "concepts_total": len(gap.per_concept),
    }

    return ValidationResult(
        ontology_key=ontology_key,
        ontology_reused=reused,
        per_claim=per_claim,
        gap_report=gap,
        quality=quality,
    )
