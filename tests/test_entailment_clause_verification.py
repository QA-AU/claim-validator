"""Per-clause re-verification of a compound claim that reads `entails` as a
whole sentence — issue #17's second finding, `docs/refactor-accuracy-demo/`'s
C1: "replaced jsonwebtoken with jose ... and better cryptographic defaults."
The library swap is true; "better defaults" isn't confirmed anywhere — and
the whole sentence still read entails, 3/3, because nothing checked the two
halves independently. See phases/entailment.py::_verify_entailed_clauses.
"""

import json

from claimvalidator.claim_shims import ResolvedClaim, _JudgeClaim
from phases.entailment import _split_into_verification_clauses, judge_entailment

CHUNKS = ["Some passage text the claims are checked against."]


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


# ---- the splitter, in isolation ---------------------------------------------


def test_a_plain_and_splits_with_no_comma_required():
    # The exact shape the retrieval-only splitter in claim_retrieval.py
    # would miss (issue #9) — this one is deliberately looser.
    clauses = _split_into_verification_clauses(
        "Replaced jsonwebtoken with jose and improved cryptographic defaults."
    )
    assert clauses == [
        "Replaced jsonwebtoken with jose",
        "improved cryptographic defaults",
    ]


def test_a_simple_claim_with_no_and_is_not_compound():
    assert _split_into_verification_clauses("The rate limit is 100 per minute.") == []


def test_an_and_inside_a_quoted_span_is_not_a_clause_boundary():
    assert _split_into_verification_clauses('The field is named "rate and limit".') == []


def test_an_and_inside_a_backtick_span_is_not_a_clause_boundary():
    assert _split_into_verification_clauses("The header is `X-Rate-And-Limit`.") == []


# ---- end to end, through judge_entailment -----------------------------------


def test_a_compound_entails_claim_is_downgraded_when_a_clause_contradicts():
    claim = ResolvedClaim(id="C1", text="X is confirmed and Y is false.", source_chunks=[0])
    client = ScriptedClient([
        _verdict_response("C1", "entails", "looked fine as a whole sentence"),
        json.dumps([
            {"id": "C1::clause0", "verdict": "entails", "reason": "X is confirmed"},
            {"id": "C1::clause1", "verdict": "contradicts", "reason": "Y is actually true"},
        ]),
    ])

    report = judge_entailment([_JudgeClaim(claim)], CHUNKS, client, runs=1)

    verdict = report.verdicts[0]
    assert verdict.verdict == "contradicts"
    assert len(verdict.clause_verdicts) == 2
    assert verdict.clause_verdicts[0]["verdict"] == "entails"
    assert verdict.clause_verdicts[1]["verdict"] == "contradicts"


def test_a_compound_entails_claim_downgrades_to_mentions_only_when_a_clause_is_unconfirmed():
    claim = ResolvedClaim(id="C1", text="X is confirmed and Y is better somehow.", source_chunks=[0])
    client = ScriptedClient([
        _verdict_response("C1", "entails"),
        json.dumps([
            {"id": "C1::clause0", "verdict": "entails", "reason": "X is confirmed"},
            {"id": "C1::clause1", "verdict": "mentions_only", "reason": "not specifically confirmed"},
        ]),
    ])

    report = judge_entailment([_JudgeClaim(claim)], CHUNKS, client, runs=1)

    assert report.verdicts[0].verdict == "mentions_only"


def test_a_compound_entails_claim_stays_entails_when_every_clause_confirms():
    claim = ResolvedClaim(id="C1", text="X is confirmed and Y is confirmed.", source_chunks=[0])
    client = ScriptedClient([
        _verdict_response("C1", "entails"),
        json.dumps([
            {"id": "C1::clause0", "verdict": "entails", "reason": "X is confirmed"},
            {"id": "C1::clause1", "verdict": "entails", "reason": "Y is confirmed"},
        ]),
    ])

    report = judge_entailment([_JudgeClaim(claim)], CHUNKS, client, runs=1)

    verdict = report.verdicts[0]
    assert verdict.verdict == "entails"
    assert len(verdict.clause_verdicts) == 2  # recorded even when nothing changed


def test_a_non_compound_entails_claim_is_never_sent_for_clause_verification():
    claim = ResolvedClaim(id="C1", text="The rate limit is 100 per minute.", source_chunks=[0])
    client = ScriptedClient([_verdict_response("C1", "entails")])

    report = judge_entailment([_JudgeClaim(claim)], CHUNKS, client, runs=1)

    assert client.calls == 1  # no second call for clause verification
    assert report.verdicts[0].clause_verdicts == []


def test_a_contradicts_verdict_is_never_sent_for_clause_verification():
    claim = ResolvedClaim(id="C1", text="X is true and Y is true.", source_chunks=[0])
    client = ScriptedClient([_verdict_response("C1", "contradicts")])

    report = judge_entailment([_JudgeClaim(claim)], CHUNKS, client, runs=1)

    assert client.calls == 1
    assert report.verdicts[0].verdict == "contradicts"


def test_disabled_by_setting_even_for_a_compound_entails_claim():
    claim = ResolvedClaim(id="C1", text="X is true and Y is false.", source_chunks=[0])
    client = ScriptedClient([_verdict_response("C1", "entails")])

    report = judge_entailment(
        [_JudgeClaim(claim)], CHUNKS, client, runs=1,
        settings={"verify_entailed_clauses": False, "escalate_undecided": False,
                  "escalate_split_contradictions": False, "escalate_split_entails": False},
    )

    assert client.calls == 1
    assert report.verdicts[0].verdict == "entails"
    assert report.verdicts[0].clause_verdicts == []
