"""run_validation's ontology_key bypass — the "pick from the shared list"
path. Only the resolution branch itself is tested here: a full run against
a real, populated ontology needs a real (or heavily stubbed) LLM client and
is covered by scripts/validate_claims.py and manual verification instead.
"""

import pytest

from claimvalidator.claim_shims import ResolvedClaim
from claimvalidator.pipeline import (
    ClaimResult,
    _agreement_label,
    _build_claim_result,
    _resolve_cited_passages,
    _resolve_cited_sources,
    run_validation,
)
from phases.entailment import EntailmentVerdict


class _FakeSearcher:
    """Stand-in for RAGIndexSearcher — only source_of() is exercised here."""

    def __init__(self, sources):
        self._sources = sources

    def source_of(self, i):
        return self._sources.get(i, "unknown")


def test_resolve_cited_sources_maps_indices_in_order():
    searcher = _FakeSearcher({0: "a.txt", 1: "b.txt"})
    assert _resolve_cited_sources([0, 1], searcher) == ["a.txt", "b.txt"]


def test_resolve_cited_sources_empty_input():
    assert _resolve_cited_sources([], _FakeSearcher({})) == []


def test_claim_result_cited_sources_defaults_to_empty_list():
    result = ClaimResult(
        id="C1", text="a claim", shape_ok=True, shape_reason=None,
        verdict="entails", judged=True, agreement="3/3", cited_chunks=[0],
        reason="r",
    )
    assert result.cited_sources == []
    assert result.to_dict()["cited_sources"] == []


def test_resolve_cited_passages_maps_indices_in_order():
    assert _resolve_cited_passages([0, 2], ["a", "b", "c"]) == ["a", "c"]


def test_resolve_cited_passages_drops_out_of_range_index():
    # Indices come from retrieval against this exact chunk list, so they
    # should always be in range — guarded anyway, and the guard must not
    # raise IndexError on a value that somehow isn't.
    assert _resolve_cited_passages([5], ["only one chunk"]) == []


def test_resolve_cited_passages_empty_input():
    assert _resolve_cited_passages([], ["a"]) == []


def test_claim_result_cited_passages_defaults_to_empty_list():
    # A field added after cited_chunks/reason (both required, no default)
    # existed — must not break a caller that predates this field.
    result = ClaimResult(
        id="C1", text="a claim", shape_ok=True, shape_reason=None,
        verdict="entails", judged=True, agreement="3/3", cited_chunks=[0],
        reason="r",
    )
    assert result.cited_passages == []
    assert result.to_dict()["cited_passages"] == []


def test_claim_result_serializes_cited_passages():
    result = ClaimResult(
        id="C1", text="a claim", shape_ok=True, shape_reason=None,
        verdict="contradicts", judged=True, agreement="3/3", cited_chunks=[0, 1],
        cited_passages=["first passage", "second passage"], reason="r",
    )
    assert result.to_dict()["cited_passages"] == ["first passage", "second passage"]


def test_claim_result_serializes_source_ref():
    result = ClaimResult(
        id="C1", text="a claim", shape_ok=True, shape_reason=None,
        verdict="entails", judged=True, agreement="3/3", cited_chunks=[0],
        reason="r", source_ref="chatbot answer, sentence 2",
    )
    assert result.to_dict()["source_ref"] == "chatbot answer, sentence 2"


def test_claim_result_source_ref_defaults_to_none_in_serialization():
    result = ClaimResult(
        id="C1", text="a claim", shape_ok=True, shape_reason=None,
        verdict="entails", judged=True, agreement="3/3", cited_chunks=[0],
        reason="r",
    )
    assert result.to_dict()["source_ref"] is None


# --- _agreement_label ---------------------------------------------------
#
# Issue #16: an EntailmentVerdict that exists but was never actually judged
# (nothing for the judge to check it against) keeps its dataclass defaults
# of agreement=1, runs_judged=1 — a hollow "1/1" this function must not
# report as if a real single-run consensus had been reached.


def test_agreement_label_is_none_for_no_verdict():
    assert _agreement_label(None) is None


def test_agreement_label_is_none_for_an_unjudged_verdict():
    verdict = EntailmentVerdict(requirement_id="C1", judged=False)
    assert _agreement_label(verdict) is None


def test_agreement_label_reports_majority_vote_as_a_fraction():
    verdict = EntailmentVerdict(requirement_id="C1", agreement=2, runs_judged=3)
    assert _agreement_label(verdict) == "2/3"


def test_agreement_label_reports_logprob_confidence_as_a_percentage():
    verdict = EntailmentVerdict(requirement_id="C1", method="logprob", confidence=0.937)
    assert _agreement_label(verdict) == "94% confidence (logprob)"


# --- _build_claim_result -------------------------------------------------
#
# Issue #16, found live: a claim with nothing for the judge to check it
# against (phases/entailment.py marks it `judged=False`, but the
# EntailmentVerdict object's other fields keep their dataclass defaults —
# verdict="entails", reason="") was reported to the API as a confident
# "entails" for a claim the system never actually checked.


def test_build_claim_result_reports_unjudged_not_entails_when_nothing_to_judge_against():
    claim = ResolvedClaim(id="C7", text="Passwords must be at least 12 characters long.")
    verdict = EntailmentVerdict(requirement_id="C7", judged=False)  # dataclass
    # defaults: verdict="entails", reason="", agreement=1, runs_judged=1

    result = _build_claim_result(claim, verdict, chunks=[], violations_by_id={},
                                  searcher=_FakeSearcher({}))

    assert result.verdict == "unjudged"
    assert result.judged is False
    assert result.agreement is None
    assert result.reason == "no citation found by retrieval"


def test_build_claim_result_reports_unjudged_when_verdict_is_none():
    """The other route to "no real verdict" — a shape violation that never
    reached the judge at all, so verdicts_by_id.get() returns nothing.
    Proves the extraction (issue #16) didn't change this existing case."""
    claim = ResolvedClaim(id="C1", text="a claim")

    result = _build_claim_result(claim, None, chunks=[], violations_by_id={},
                                  searcher=_FakeSearcher({}))

    assert result.verdict == "unjudged"
    assert result.judged is False
    assert result.agreement is None
    assert result.reason == "no citation found by retrieval"


def test_build_claim_result_passes_through_a_real_judged_verdict():
    claim = ResolvedClaim(id="C1", text="a claim", source_chunks=[0])
    verdict = EntailmentVerdict(
        requirement_id="C1", verdict="contradicts", reason="the passage says otherwise",
        agreement=3, runs_judged=3,
    )

    result = _build_claim_result(claim, verdict, chunks=["passage text"],
                                  violations_by_id={}, searcher=_FakeSearcher({0: "doc.txt"}))

    assert result.verdict == "contradicts"
    assert result.judged is True
    assert result.agreement == "3/3"
    assert result.reason == "the passage says otherwise"
    assert result.cited_passages == ["passage text"]
    assert result.cited_sources == ["doc.txt"]


def test_ontology_key_for_a_nonexistent_ontology_raises_clearly(tmp_path):
    with pytest.raises(ValueError, match="No such ontology"):
        run_validation(
            workflow_id="wf-test",
            document_paths=[],
            claims_input=[{"id": "C1", "text": "a claim"}],
            llm_client=object(),  # never reached — resolution fails first
            store_root=str(tmp_path),
            ontology_key="does-not-exist",
        )
