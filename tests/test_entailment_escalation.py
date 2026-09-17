"""`_needs_escalation` — which verdicts get a second opinion from a stronger
model. Previously untested directly; only exercised indirectly through
`judge_entailment`'s full pipeline. Isolated here because the function itself
takes nothing but a plain `EntailmentVerdict` and a settings dict — no client,
no retrieval, no reason to route it through the expensive path to test it.
"""

from phases.entailment import (
    VERDICT_CONTRADICTS,
    VERDICT_ENTAILS,
    VERDICT_MENTIONS_ONLY,
    VERDICT_NO_EVIDENCE,
    EntailmentVerdict,
    _needs_escalation,
)

_ON = {"escalate_split_contradictions": True, "escalate_split_entails": True}
_OFF = {"escalate_split_contradictions": False, "escalate_split_entails": False}


def _verdict(verdict, agreement, runs_judged=3):
    return EntailmentVerdict(
        requirement_id="C1", verdict=verdict, agreement=agreement, runs_judged=runs_judged
    )


def test_unanimous_contradicts_does_not_escalate():
    assert not _needs_escalation(_verdict(VERDICT_CONTRADICTS, 3), _ON)


def test_split_contradicts_escalates_when_enabled():
    assert _needs_escalation(_verdict(VERDICT_CONTRADICTS, 2), _ON)


def test_split_contradicts_does_not_escalate_when_disabled():
    assert not _needs_escalation(_verdict(VERDICT_CONTRADICTS, 2), _OFF)


def test_unanimous_entails_does_not_escalate():
    assert not _needs_escalation(_verdict(VERDICT_ENTAILS, 3), _ON)


def test_split_entails_escalates_when_enabled():
    """Issue #17: a bare-majority "prevents race conditions" call, the exact
    shape the fix targets, must reach a stronger model."""
    assert _needs_escalation(_verdict(VERDICT_ENTAILS, 2), _ON)


def test_split_entails_does_not_escalate_when_disabled():
    assert not _needs_escalation(_verdict(VERDICT_ENTAILS, 2), _OFF)


def test_split_mentions_only_never_escalates():
    """Deliberately excluded even when split — still a non-committal finding,
    same as before this fix; only entails/contradicts are accusations or
    confirmations worth a second opinion."""
    assert not _needs_escalation(_verdict(VERDICT_MENTIONS_ONLY, 2), _ON)


def test_split_no_evidence_never_escalates():
    assert not _needs_escalation(_verdict(VERDICT_NO_EVIDENCE, 2), _ON)


def test_an_unjudged_verdict_never_escalates():
    unjudged = EntailmentVerdict(requirement_id="C1", judged=False)
    assert not _needs_escalation(unjudged, _ON)


def test_an_undecided_verdict_escalates_regardless_of_split_settings():
    undecided = _verdict(VERDICT_ENTAILS, 1, runs_judged=3)  # 1/3: no majority
    assert _needs_escalation(undecided, {"escalate_undecided": True, **_OFF})
