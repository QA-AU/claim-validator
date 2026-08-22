"""claimvalidator/logprob_judge.py — the one-call, confidence-from-tokens
alternative to the majority-vote judge. Covers the same shim duck-typing
risk test_entailment_shim.py covers for the majority-vote path, plus the
part unique to this module: reading a verdict and its confidence out of a
raw Ollama-shaped logprobs response.
"""

import pytest

from claimvalidator.claim_shims import ResolvedClaim, _JudgeClaim
from claimvalidator.logprob_judge import (
    LogprobsUnsupportedError,
    _extract_confidence,
    judge_entailment_logprob,
)
from phases.ollama_client import LogprobResponse

CHUNKS = ["The client_id parameter is REQUIRED for all authorization requests."]


class ScriptedLogprobClient:
    """Exposes generate_with_logprobs, the capability judge_entailment_logprob
    is gated on — a plain ScriptedClient (generate only) must never reach
    this code path, which test_pipeline-level gating covers separately."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = 0

    def generate_with_logprobs(self, prompt, system_prompt=None, temperature=None,
                                top_logprobs=5):
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


def _tokens_for(word: str, logprob: float, alternatives=None):
    """One-token response shaped like Ollama's real logprobs field."""
    top = [{"token": word, "logprob": logprob}]
    for alt_word, alt_logprob in (alternatives or []):
        top.append({"token": alt_word, "logprob": alt_logprob})
    return [{"token": word, "logprob": logprob, "top_logprobs": top}]


def test_a_confident_single_candidate_reads_as_high_confidence():
    tokens = _tokens_for("ENTAILS", -0.05)

    verdict, probabilities, raw = _extract_confidence(tokens)

    assert verdict == "entails"
    assert probabilities["entails"] > 0.9
    assert raw == "ENTAILS"


def test_confidence_is_normalized_across_the_four_candidates_not_the_full_vocabulary():
    """Ollama's top_logprobs are drawn from the whole vocabulary — most of
    that mass is words that were never a real answer. Confidence should be
    conditioned on it being one of the four verdict words, not diluted by
    unrelated tokens sitting in the same top-N list."""
    tokens = _tokens_for("ENTAILS", -0.7, alternatives=[
        ("MENTIONS_ONLY", -1.2), ("the", -3.0), ("a", -4.0),
    ])

    verdict, probabilities, _ = _extract_confidence(tokens)

    assert verdict == "entails"
    assert set(probabilities) == {"entails", "mentions_only"}
    assert abs(sum(probabilities.values()) - 1.0) < 1e-6


def test_a_genuinely_split_call_reports_middling_confidence_not_manufactured_certainty():
    tokens = _tokens_for("ENTAILS", -0.7, alternatives=[("MENTIONS_ONLY", -0.72)])

    verdict, probabilities, _ = _extract_confidence(tokens)

    assert verdict == "entails"
    assert 0.45 < probabilities["entails"] < 0.55  # near coin-flip, honestly reported


def test_no_tokens_at_all_reports_no_verdict_rather_than_guessing():
    verdict, probabilities, raw = _extract_confidence([])

    assert verdict is None
    assert probabilities == {}
    assert raw is None


def test_judge_reaches_a_verdict_through_the_shim():
    claim = ResolvedClaim(id="C1", text="client_id is required.", source_chunks=[0])
    client = ScriptedLogprobClient([
        LogprobResponse(text="ENTAILS", tokens=_tokens_for("ENTAILS", -0.1)),
    ])

    report = judge_entailment_logprob([_JudgeClaim(claim)], CHUNKS, client)

    assert len(report.entailed) == 1
    verdict = report.entailed[0]
    assert verdict.method == "logprob"
    assert verdict.confidence > 0.9
    assert verdict.runs_judged == 1  # one call, not three — the whole point


def test_a_claim_with_no_citation_is_unjudgeable_not_accused():
    claim = ResolvedClaim(id="C1", text="No supporting passage was ever found.", source_chunks=[])
    client = ScriptedLogprobClient([])  # never called — nothing to judge

    report = judge_entailment_logprob([_JudgeClaim(claim)], CHUNKS, client)

    assert len(report.unjudgeable) == 1
    assert report.verdicts[0].judged is False


def test_a_call_that_errors_stays_silent_not_accusing():
    claim = ResolvedClaim(id="C1", text="A claim whose call errors out.", source_chunks=[0])

    class BrokenClient:
        def generate_with_logprobs(self, prompt, system_prompt=None, temperature=None,
                                    top_logprobs=5):
            raise RuntimeError("simulated model failure")

    report = judge_entailment_logprob([_JudgeClaim(claim)], CHUNKS, BrokenClient())

    assert report.failed_batches >= 1
    assert not any(v.requirement_id == "C1" and v.judged for v in report.verdicts)


def test_missing_logprobs_on_the_only_claim_raises_rather_than_silently_degrading():
    """Found live: Ollama's cloud-hosted models answer but never populate
    `logprobs` at all — a property of the model, not detectable from the
    client class in advance. This must surface as a clear, catchable
    failure so a caller in "auto" mode can retry with majority_vote, rather
    than reporting "confidence unavailable" on every claim for the rest of
    the run without ever saying why."""
    claim = ResolvedClaim(id="C1", text="client_id is required.", source_chunks=[0])
    client = ScriptedLogprobClient([
        LogprobResponse(text="ENTAILS", tokens=[]),
    ])

    with pytest.raises(LogprobsUnsupportedError):
        judge_entailment_logprob([_JudgeClaim(claim)], CHUNKS, client)


def test_logprobs_present_but_never_matching_a_verdict_also_raises():
    """The qwen3.8 case: logprobs came back non-empty, but the token was
    "The" — the start of a reasoning trace, not the answer. Empty tokens
    and unmatchable-but-present tokens are the same failure from a caller's
    point of view (no usable confidence), so both must raise the same way."""
    claim = ResolvedClaim(id="C1", text="client_id is required.", source_chunks=[0])
    client = ScriptedLogprobClient([
        LogprobResponse(text="ENTAILS", tokens=_tokens_for("The", -0.01)),
    ])

    with pytest.raises(LogprobsUnsupportedError):
        judge_entailment_logprob([_JudgeClaim(claim)], CHUNKS, client)


def test_missing_logprobs_is_only_checked_on_the_first_real_call():
    """A later claim's logprobs coming back empty (a genuine parse miss, not
    a systemic capability gap) must not abort claims already judged fine —
    only the very first successful call decides whether the client can do
    this at all."""
    claim1 = ResolvedClaim(id="C1", text="client_id is required.", source_chunks=[0])
    claim2 = ResolvedClaim(id="C2", text="Another claim.", source_chunks=[0])
    client = ScriptedLogprobClient([
        LogprobResponse(text="ENTAILS", tokens=_tokens_for("ENTAILS", -0.1)),
        LogprobResponse(text="ENTAILS", tokens=[]),
    ])

    report = judge_entailment_logprob(
        [_JudgeClaim(claim1), _JudgeClaim(claim2)], CHUNKS, client,
    )

    assert len(report.entailed) == 2  # both still judged from their response text
    by_id = {v.requirement_id: v for v in report.verdicts}
    assert by_id["C1"].confidence > 0.9
    assert by_id["C2"].confidence is None  # this one's logprobs happened to be empty
