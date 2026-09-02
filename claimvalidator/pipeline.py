"""Ties retrieval, shape check, entailment judge and the gap report into one
call — used identically by the CLI script and the async job worker, so the
HTTP layer adds no business logic of its own, only request/response
marshalling.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from phases.entailment import judge_entailment
from phases.llm_usage import Usage, usage_of
from phases.ontology_store import OntologyStore
from phases.phase1_models import Ontology
from phases.phase1_orchestrator import run_phase1
from phases.requirement_shapes import check_requirement_shapes
from phases.run_tracker import RunTracker

from claimvalidator import config
from claimvalidator.claim_retrieval import retrieve_for_claim
from claimvalidator.claim_shims import ResolvedClaim, _ClaimSet, _JudgeClaim, shape_profile
from claimvalidator.document_identity import resolve_ontology_key
from claimvalidator.gap_report import GapReport, build_gap_report
from claimvalidator.logprob_judge import LogprobsUnsupportedError, judge_entailment_logprob

logger = logging.getLogger(__name__)


def _usage_snapshot(*llm_clients) -> Dict[str, int]:
    """The three running totals `usage_of` reports, copied out as plain ints
    and summed across every distinct client passed in.

    `usage_of` hands back the client's live, mutating `Usage` object — the
    same instance every call — so a "before" snapshot must copy the numbers
    out rather than keep the reference, or it would silently track "after"
    too. Variadic (not just one client) because judging can run against a
    client of its own, separate from the one used for everything else — see
    `judge_llm_client` below; deduped by identity so passing the same
    client twice (the common case, when no override is given) doesn't
    double-count it.
    """
    total = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    seen_ids = set()
    for client in llm_clients:
        if client is None or id(client) in seen_ids:
            continue
        seen_ids.add(id(client))
        u = usage_of(client)
        total["calls"] += u.calls
        total["input_tokens"] += u.input_tokens
        total["output_tokens"] += u.output_tokens
    return total


def _model_label(client) -> str:
    """"OllamaClient (llama3.2:latest)" — provider inferred from the class
    rather than a separate field nothing sets, model read from the one
    attribute both clients already expose. "unknown" for a client that
    exposes neither (a test stub, typically), rather than raising — a
    report missing a model name is a smaller problem than a report that
    can't be built at all."""
    model = getattr(client, "model", None)
    provider = client.__class__.__name__
    return f"{provider} ({model})" if model else provider


def _agreement_label(verdict) -> Optional[str]:
    """"2/3" for a majority-vote verdict, "94% confidence (logprob)" for a
    logprob one — a bucketed vote count and a continuous probability are
    different kinds of number, so they get visibly different labels rather
    than forcing the logprob path's confidence through an N/N format that
    would always read as a hollow 1/1."""
    if verdict is None:
        return None
    if verdict.method == "logprob":
        if verdict.confidence is None:
            return "logprob (confidence unavailable)"
        return f"{round(verdict.confidence * 100)}% confidence (logprob)"
    return f"{verdict.agreement}/{verdict.runs_judged}"


def _usage_delta(before: Dict[str, int], after: Dict[str, int], rates) -> Dict[str, Any]:
    """What one phase spent: the difference between two snapshots of the same
    client, so a run with several phases can say which one was expensive
    instead of only what the whole run cost."""
    calls = after["calls"] - before["calls"]
    input_tokens = after["input_tokens"] - before["input_tokens"]
    output_tokens = after["output_tokens"] - before["output_tokens"]
    cost = rates.cost_cents(input_tokens, output_tokens) if rates is not None else None
    return {
        "calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_cents": round(cost, 4) if cost is not None else "not available",
    }


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
    # Per-claim escalation detail — without this, the aggregate `escalated`
    # count in quality says *how many* verdicts a stronger model settled, but
    # not *which* claim or what it changed from, which is the whole point of
    # showing escalation results to a person rather than just a number.
    escalated: bool = False
    escalated_from: str = ""
    escalation_model: str = ""
    # How the verdict was reached — "majority_vote" (default) or "logprob"
    # (claimvalidator/logprob_judge.py) — and, only for the latter, the
    # model's own confidence. A bucketed N/3 agreement count and a continuous
    # probability are different kinds of number; kept in separate fields
    # rather than one overloaded to mean either. See EntailmentVerdict.
    judge_method: str = "majority_vote"
    confidence: Optional[float] = None
    # Did a strict majority of the judge's runs agree on `verdict`? True for
    # an unjudged claim (there was no split to have) and for a logprob
    # verdict (one call, nothing to disagree with itself over) — only a
    # majority-vote claim can actually be False here. Needed so the Quality
    # tab's `undecided` count can name which claim IDs it means, the same
    # way `escalated` already can via escalated/escalated_from above.
    decided: bool = True
    # See ClaimInput.source_ref's docstring — pure pass-through, never
    # read by anything upstream of here.
    source_ref: Optional[str] = None

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
            "escalated": self.escalated,
            "escalated_from": self.escalated_from,
            "escalation_model": self.escalation_model,
            "judge_method": self.judge_method,
            "confidence": self.confidence,
            "decided": self.decided,
            "source_ref": self.source_ref,
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
    judge_method: str = "auto",
    judge_llm_client=None,
    ontology_key: Optional[str] = None,
    created_by: str = "",
) -> ValidationResult:
    """
    `ontology_key`: when given, skips document-based ontology resolution
    entirely and validates against this existing, already-built ontology
    directly — the "pick from the shared list" path (see
    claimvalidator/document_identity.py's own resolution path for the
    default, content-hash-based one). `document_paths` may be empty in
    this case: nothing after ontology resolution reads from it, only from
    the resolved ontology's own persisted chunks. Raises `ValueError` if
    no ontology exists under this key.

    `judge_method`: "auto" uses the one-call, logprob-confidence judge
    (`claimvalidator/logprob_judge.py`) when the judging client supports it
    (currently Ollama only — Anthropic's API does not expose token
    probabilities) and falls back to the existing 3-run majority vote
    otherwise; "majority_vote" always forces the existing path;
    "logprob" always forces the new one, raising if the client can't do it.
    Gated on capability rather than provider name, so a client either can or
    can't — nothing here special-cases a provider by name.

    `judge_llm_client`: optional, defaults to reusing `llm_client`. Judging
    is a different job from the rest of the pipeline — one short,
    constrained classification per claim — and a thinking-capable model
    that is a good choice for ontology extraction or the census can be a
    poor choice specifically for the logprob judge: Ollama's `/api/generate`
    does not reliably honour `think: false` for every hybrid model, and a
    thinking model's chain-of-thought tokens can end up in `logprobs`
    instead of its actual answer, which `judge_entailment_logprob` detects
    and raises on (see LogprobsUnsupportedError) rather than silently
    reporting bad confidence. Passing a second, plain instruction-following
    client here — e.g. a small local Ollama model — lets the rest of the
    run keep whatever model `llm_client` is configured with while judging
    uses one that actually answers "one word, no reasoning" reliably.
    """
    rates = config.token_rates()
    # Per-phase spend, keyed to match the RunTracker phase_name suffixes below
    # (claim_ontology -> "ontology", etc.) — populated as each phase finishes,
    # so a slow or expensive run says *which* phase cost the tokens instead of
    # only what the whole run cost in aggregate (see `quality["llm_calls"]`
    # etc. below).
    phase_usage: Dict[str, Dict[str, Any]] = {}

    tracker = RunTracker(db_session, workflow_id, name=document_id or "validation",
                          phase_name="claim_ontology")
    tracker.start()

    store = OntologyStore(root=store_root)
    tracker.step_start("resolve_ontology")
    if ontology_key is not None:
        # Explicit pick-from-list path — the caller already knows which
        # ontology it wants, so there's nothing to resolve from documents.
        if store.load_meta(ontology_key) is None:
            raise ValueError(f"No such ontology: {ontology_key}")
        reused = True
    else:
        ontology_key, reused = resolve_ontology_key(
            store, document_paths, document_id, created_by=created_by
        )
    tracker.step_complete("resolve_ontology", ontology_key=ontology_key, reused=reused)

    ontology_usage_before = _usage_snapshot(llm_client)
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
    phase_usage["ontology"] = _usage_delta(ontology_usage_before, _usage_snapshot(llm_client), rates)

    ontology = Ontology.from_dict(store.load_current(ontology_key))
    searcher = store.searcher_for(ontology_key)
    index = store.load_index(ontology_key)
    chunks: List[str] = index["chunks"]
    source_text = "\n\n".join(chunks)  # approximate — chunks overlap, fine for section-matching

    tracker.finish(
        "success",
        tokens_used=phase_usage["ontology"]["total_tokens"],
        cost_cents=phase_usage["ontology"]["cost_cents"] if rates is not None else None,
    )

    claims = [ResolvedClaim(id=c["id"], text=c["text"], source_ref=c.get("source_ref"))
              for c in claims_input]

    retrieval_tracker = RunTracker(db_session, workflow_id, name=document_id or "validation",
                                    phase_name="claim_retrieval")
    retrieval_tracker.start()
    retrieval_tracker.step_start("retrieve")
    retrieval_usage_before = _usage_snapshot(llm_client)
    found_nothing = 0
    for claim in claims:
        result = retrieve_for_claim(claim.text, ontology, searcher, llm_client)
        claim.source_chunks = result.chunk_indices
        if not result.chunk_indices:
            found_nothing += 1
    phase_usage["retrieval"] = _usage_delta(retrieval_usage_before, _usage_snapshot(llm_client), rates)
    retrieval_tracker.step_complete("retrieve", claims=len(claims), found_nothing=found_nothing)
    retrieval_tracker.finish(
        "success",
        tokens_used=phase_usage["retrieval"]["total_tokens"],
        cost_cents=phase_usage["retrieval"]["cost_cents"] if rates is not None else None,
    )

    shape_tracker = RunTracker(db_session, workflow_id, name=document_id or "validation",
                                phase_name="claim_shape_check")
    shape_tracker.start()
    shape_report = check_requirement_shapes(
        _ClaimSet(claims), profile=shape_profile(shape_rule_overrides), tracker=shape_tracker,
    )
    violations_by_id = {v.item_id: v.reason for v in shape_report.violations}
    # No LLM calls in this phase — it's a deterministic text check — so there
    # is nothing to snapshot; recorded as zero rather than left out, the same
    # "absent would look like it was never measured" reasoning the census
    # module uses for a concept with no instances.
    phase_usage["shape_check"] = _usage_delta(
        {"calls": 0, "input_tokens": 0, "output_tokens": 0},
        {"calls": 0, "input_tokens": 0, "output_tokens": 0},
        rates,
    )
    shape_tracker.finish("success")

    judge_tracker = RunTracker(db_session, workflow_id, name=document_id or "validation",
                                phase_name="claim_entailment")
    judge_tracker.start()
    # A separate client, when given, means judging spends its own tokens —
    # snapshot both so `phase_usage["entailment"]` and the run's aggregate
    # totals below still add up to everything actually spent.
    judge_client = judge_llm_client or llm_client
    judge_usage_before = _usage_snapshot(llm_client, judge_client)

    supports_logprobs = hasattr(judge_client, "generate_with_logprobs")
    if judge_method == "logprob" and not supports_logprobs:
        raise ValueError(
            "judge_method='logprob' but the judging client has no "
            "generate_with_logprobs — only Ollama supports it currently"
        )
    use_logprob_judge = judge_method == "logprob" or (judge_method == "auto" and supports_logprobs)

    if use_logprob_judge:
        try:
            entailment_report = judge_entailment_logprob(
                [_JudgeClaim(c) for c in claims], chunks, judge_client,
            )
        except LogprobsUnsupportedError as e:
            if judge_method == "logprob":
                raise  # explicitly forced — a silent fallback would hide it
            logger.warning(
                f"[Pipeline] logprob judge unusable ({e}); falling back to "
                f"majority_vote for this run"
            )
            use_logprob_judge = False
            entailment_report = judge_entailment(
                [_JudgeClaim(c) for c in claims], chunks, judge_client, db_session=db_session,
            )
    else:
        entailment_report = judge_entailment(
            [_JudgeClaim(c) for c in claims], chunks, judge_client, db_session=db_session,
        )
    verdicts_by_id = {v.requirement_id: v for v in entailment_report.verdicts}
    phase_usage["entailment"] = _usage_delta(
        judge_usage_before, _usage_snapshot(llm_client, judge_client), rates
    )
    judge_tracker.step_complete(
        "judge",
        judged=len(entailment_report.judged),
        entailed=len(entailment_report.entailed),
        mentions_only=len(entailment_report.mentions_only),
        contradicted=len(entailment_report.contradicted),
        no_evidence=len(entailment_report.no_evidence),
    )
    judge_tracker.finish(
        "success",
        tokens_used=phase_usage["entailment"]["total_tokens"],
        cost_cents=phase_usage["entailment"]["cost_cents"] if rates is not None else None,
    )

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
            agreement=_agreement_label(verdict),
            cited_chunks=claim.source_chunks,
            reason=(verdict.reason if verdict else "no citation found by retrieval"),
            escalated=bool(verdict and verdict.escalated),
            escalated_from=(verdict.escalated_from if verdict else ""),
            escalation_model=(verdict.escalation_model if verdict else ""),
            judge_method=(verdict.method if verdict else "majority_vote"),
            confidence=(verdict.confidence if verdict else None),
            decided=(verdict.decided if verdict else True),
            source_ref=claim.source_ref,
        ))

    completeness_tracker = RunTracker(db_session, workflow_id, name=document_id or "validation",
                                       phase_name="claim_completeness")
    completeness_tracker.start()
    completeness_usage_before = _usage_snapshot(llm_client)
    gap = build_gap_report(
        ontology, chunks, llm_client, claims, source_text=source_text,
        runs=census_runs, max_chunks=census_max_chunks, force=force_census,
        db_session=db_session,
    )
    phase_usage["completeness"] = _usage_delta(
        completeness_usage_before, _usage_snapshot(llm_client), rates
    )
    completeness_tracker.step_complete("gap_report", ran=gap.ran,
                                        concepts=len(gap.per_concept))
    completeness_tracker.finish(
        "success" if gap.ran else "skipped",
        tokens_used=phase_usage["completeness"]["total_tokens"],
        cost_cents=phase_usage["completeness"]["cost_cents"] if rates is not None else None,
    )

    undecided = sum(1 for v in entailment_report.judged if not v.decided)
    escalated = sum(1 for v in entailment_report.verdicts if v.escalated)
    overturned = sum(
        1 for v in entailment_report.verdicts
        if v.escalated and v.escalated_from and v.escalated_from != v.verdict
    )

    # Almost always one client throughout — build/reuse, retrieval, judging,
    # census — so its accumulated usage is the whole run's cost. Combined
    # with `judge_client` here too, since when `judge_llm_client` was given,
    # its spend is otherwise invisible to this total: `usage_of(llm_client)`
    # alone would silently undercount by whatever judging spent on its own
    # client. Built from a fresh snapshot rather than summed from
    # `phase_usage` above, so a bug in one phase's snapshot can't throw off
    # the number that actually matters most.
    combined = _usage_snapshot(llm_client, judge_client)
    usage = Usage(calls=combined["calls"], input_tokens=combined["input_tokens"],
                  output_tokens=combined["output_tokens"])
    usage_dict = usage.to_dict(rates)

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
        "undecided": undecided,
        "escalated": escalated,
        "escalation_failed_batches": entailment_report.escalation_failed_batches,
        "overturned": overturned,
        "main_model": _model_label(llm_client),
        "judge_model": _model_label(judge_client),
        "judge_method": "logprob" if use_logprob_judge else "majority_vote",
        "runs": entailment_report.runs,
        "concepts_covered": sum(1 for g in gap.per_concept.values() if g.addressed_count > 0),
        "concepts_total": len(gap.per_concept),
        "llm_calls": usage_dict["calls"],
        "input_tokens": usage_dict["input_tokens"],
        "output_tokens": usage_dict["output_tokens"],
        "total_tokens": usage_dict["total_tokens"],
        "cost_cents": usage_dict["cost_cents"] if usage_dict["cost_available"] else "not available",
        "usage_by_phase": phase_usage,
    }

    return ValidationResult(
        ontology_key=ontology_key,
        ontology_reused=reused,
        per_claim=per_claim,
        gap_report=gap,
        quality=quality,
    )
