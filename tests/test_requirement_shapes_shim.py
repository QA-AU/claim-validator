"""Proves check_requirement_shapes (unmodified, from the source repo) actually
runs correctly against `_ClaimSet`/`_ShapeClaim` — the shape-check half of
the same duck-typing risk test_entailment_shim.py covers for the judge.
"""

from phases.requirement_shapes import check_requirement_shapes

from claimvalidator.claim_shims import ResolvedClaim, _ClaimSet, shape_profile


def test_a_real_claim_passes_the_default_rules():
    claims = [ResolvedClaim(id="C1", text="A properly long and checkable claim about the API.")]
    report = check_requirement_shapes(_ClaimSet(claims), profile=shape_profile(None))
    assert report.checked == 1
    assert len(report.violations) == 0


def test_a_too_short_claim_is_flagged():
    claims = [ResolvedClaim(id="C1", text="too short")]
    report = check_requirement_shapes(_ClaimSet(claims), profile=shape_profile(None))
    assert len(report.violations) == 1
    assert report.violations[0].item_id == "C1"


def test_require_subject_override_flags_every_bare_claim():
    # A specialist task's override — the shim has no `endpoint` concept for a
    # bare claim, so turning require_subject on should flag everything.
    claims = [ResolvedClaim(id="C1", text="A perfectly fine, long enough claim to judge.")]
    report = check_requirement_shapes(
        _ClaimSet(claims), profile=shape_profile({"require_subject": True}),
    )
    assert len(report.violations) == 1
    assert "subject" in report.violations[0].reason


def test_default_never_requires_a_subject():
    claims = [ResolvedClaim(id="C1", text="A perfectly fine, long enough claim to judge.")]
    report = check_requirement_shapes(_ClaimSet(claims), profile=shape_profile(None))
    assert len(report.violations) == 0
