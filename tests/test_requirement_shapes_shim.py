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


def test_invalid_subject_pattern_is_a_violation_not_an_exception():
    # A broken regex reaching this function (a hand-edited meta.json, or a
    # bug upstream of the Pydantic/inference-level guards) must never raise
    # re.error out of the shape check itself — it's flagged as a violation
    # of the *rule*, same as any other unusable configuration. Uses a
    # SimpleNamespace with a real .endpoint rather than the bare-claim shim,
    # since _ShapeClaim never sets .endpoint at all (the "no subject" branch
    # would fire first and never reach subject_pattern) — this is testing
    # requirement_shapes.py's own defense, independent of that shim's gap.
    from types import SimpleNamespace

    item = SimpleNamespace(
        id="C1", title="A requirement", expected_behavior="Something checkable.",
        criteria=[], endpoint="GET /orders/{id}",
    )
    claim_set = SimpleNamespace(requirements=[item])
    report = check_requirement_shapes(
        claim_set,
        profile=shape_profile({"require_subject": True, "subject_pattern": "(unclosed"}),
    )
    assert len(report.violations) == 1
    assert "not usable" in report.violations[0].reason


def test_non_string_id_pattern_is_a_violation_not_an_exception():
    claims = [ResolvedClaim(id="C1", text="A perfectly fine, long enough claim to judge.")]
    report = check_requirement_shapes(
        _ClaimSet(claims), profile=shape_profile({"id_pattern": 12345}),
    )
    assert len(report.violations) == 1
    assert "not usable" in report.violations[0].reason


def test_a_real_pattern_still_matches_correctly():
    # The hardening in requirement_shapes.py must not change behavior for a
    # rule that was never broken in the first place.
    claims = [ResolvedClaim(id="REQ-1", text="A perfectly fine, long enough claim to judge.")]
    report = check_requirement_shapes(
        _ClaimSet(claims), profile=shape_profile({"id_pattern": r"^REQ-\d+$"}),
    )
    assert len(report.violations) == 0


def test_ontology_profile_used_as_base_when_no_request_override():
    # Proves claim_shims.shape_profile's new `base` parameter actually
    # threads an ontology-supplied profile through, independent of any
    # per-request override.
    claims = [ResolvedClaim(id="C1", text="too short")]
    base = {"requirement": {"require_fields": ["title"], "require_any_of": ["expected_behavior"]}}
    report = check_requirement_shapes(
        _ClaimSet(claims), profile=shape_profile(None, base=base),
    )
    assert len(report.violations) == 1  # still too short — base rules were applied, not ignored


def test_request_override_still_wins_over_ontology_base():
    claims = [ResolvedClaim(id="C1", text="A perfectly fine, long enough claim to judge.")]
    base = {"requirement": {"require_subject": True}}
    # No override given — the ontology's own (deliberately dangerous, for
    # this test) base rule should still apply.
    report = check_requirement_shapes(_ClaimSet(claims), profile=shape_profile(None, base=base))
    assert len(report.violations) == 1
    assert "subject" in report.violations[0].reason
