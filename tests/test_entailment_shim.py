"""Proves `_JudgeClaim` actually satisfies `judge_entailment`'s duck-typing —
not just that the shim's attributes look right in isolation (test_claim_shims.py
covers that), but that the real judge function runs against it end to end and
produces a correct verdict. This is the automated version of the risk the
plan named highest: does the shim satisfy the target module, or only look
like it does.

Not a port of the source repo's 921-line test_entailment.py — that file
builds its fixtures from `phases.requirements_generator.TestRequirement`,
which this repo deliberately doesn't carry (generation is out of scope
here). These tests cover the same three properties its docstring names as
load-bearing (`mentions_only` reachable and distinct from `contradicts`, an
unjudgeable claim is never accused, a broken judge stays silent), through
this repo's own shim instead.
"""

import json

from claimvalidator.claim_shims import ResolvedClaim, _JudgeClaim
from phases.entailment import judge_entailment

CHUNKS = ["The client_id parameter is REQUIRED for all authorization requests."]


class ScriptedClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = 0

    def generate(self, prompt, system_prompt=None):
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


def _verdict_response(claim_id, verdict, reason="scripted"):
    return json.dumps([{"id": claim_id, "verdict": verdict, "reason": reason}])


def test_entails_reached_through_the_shim():
    claim = ResolvedClaim(id="C1", text="client_id is required.", source_chunks=[0])
    client = ScriptedClient([_verdict_response("C1", "entails")] * 3)

    report = judge_entailment([_JudgeClaim(claim)], CHUNKS, client)

    assert len(report.entailed) == 1
    assert report.entailed[0].requirement_id == "C1"


def test_mentions_only_is_distinct_from_contradicts():
    claim = ResolvedClaim(id="C1", text="A related but unstated claim.", source_chunks=[0])
    client = ScriptedClient([_verdict_response("C1", "mentions_only")] * 3)

    report = judge_entailment([_JudgeClaim(claim)], CHUNKS, client)

    assert len(report.mentions_only) == 1
    assert len(report.contradicted) == 0


def test_claim_with_no_citation_is_unjudgeable_not_accused():
    claim = ResolvedClaim(id="C1", text="No supporting passage was ever found.", source_chunks=[])
    client = ScriptedClient(["[]"])  # never actually called — nothing to judge

    report = judge_entailment([_JudgeClaim(claim)], CHUNKS, client)

    assert len(report.unjudgeable) == 1
    assert len(report.judged) == 0
    verdict = report.verdicts[0]
    assert verdict.judged is False
    # Not fabricated as "entails" or any accusation — the default construction
    # leaves `verdict` at its dataclass default, but `judged=False` is what
    # every caller must actually branch on.


def test_a_batch_that_fails_every_call_stays_silent_not_accusing():
    claim = ResolvedClaim(id="C1", text="A claim whose judge call errors out.", source_chunks=[0])

    class BrokenClient:
        def generate(self, prompt, system_prompt=None):
            raise RuntimeError("simulated model failure")

    report = judge_entailment([_JudgeClaim(claim)], CHUNKS, BrokenClient())

    assert report.failed_batches >= 1
    # No verdict at all for C1 — a failed judge must not manufacture a finding.
    assert not any(v.requirement_id == "C1" and v.judged for v in report.verdicts)


def test_majority_verdict_settles_a_split_across_runs():
    claim = ResolvedClaim(id="C1", text="A genuinely borderline claim.", source_chunks=[0])
    client = ScriptedClient([
        _verdict_response("C1", "entails"),
        _verdict_response("C1", "entails"),
        _verdict_response("C1", "mentions_only"),
    ])

    report = judge_entailment([_JudgeClaim(claim)], CHUNKS, client)

    verdict = report.verdicts[0]
    assert verdict.verdict == "entails"  # 2 of 3 runs
    assert verdict.runs_judged == 3
