"""The shims are the highest-risk new code: they exist purely to satisfy
duck-typing in modules that were never changed to know about bare claims.
These tests check that a bare id+text claim actually produces the attributes
`check_requirement_shapes` and `judge_entailment` read.
"""

from claimvalidator.claim_shims import (
    BARE_CLAIM_RULES,
    MIN_CLAIM_CHARS,
    ResolvedClaim,
    _ClaimSet,
    _JudgeClaim,
    _ShapeClaim,
    resolve_shape_rules,
    shape_profile,
)


def test_shape_claim_carries_text_as_expected_behavior():
    claim = ResolvedClaim(id="C1", text="A sufficiently long claim to be checkable.")
    shim = _ShapeClaim(claim)
    assert shim.id == "C1"
    assert shim.title  # non-empty, satisfies require_fields=["title"]
    assert shim.expected_behavior == claim.text
    assert shim.criteria == []


def test_shape_claim_under_min_chars_has_nothing_checkable():
    claim = ResolvedClaim(id="C1", text="too short")
    assert len(claim.text) < MIN_CLAIM_CHARS
    shim = _ShapeClaim(claim)
    assert shim.expected_behavior == ""  # require_any_of fails on this


def test_judge_claim_carries_verbatim_text_and_citations():
    claim = ResolvedClaim(id="C1", text="The claim text.", source_chunks=[3, 7])
    shim = _JudgeClaim(claim)
    assert shim.id == "C1"
    assert shim.title == "The claim text."  # unparaphrased — _claim_of() renders this first
    assert shim.expected_behavior == ""
    assert shim.criteria == []
    assert shim.source_chunks == [3, 7]


def test_claim_set_wraps_a_requirements_list():
    claims = [ResolvedClaim(id="C1", text="x" * 20), ResolvedClaim(id="C2", text="y" * 20)]
    wrapped = _ClaimSet(claims)
    assert len(wrapped.requirements) == 2
    assert all(isinstance(r, _ShapeClaim) for r in wrapped.requirements)


def test_default_rules_require_no_subject():
    # A bare claim has no endpoint/subject concept — require_subject must be
    # False, or check_requirement_shapes would flag every claim as defective.
    assert BARE_CLAIM_RULES["requirement"]["require_subject"] is False


def test_resolve_shape_rules_with_no_override_returns_default():
    assert resolve_shape_rules(None) == BARE_CLAIM_RULES
    assert resolve_shape_rules({}) == BARE_CLAIM_RULES


def test_resolve_shape_rules_merges_overrides_without_dropping_defaults():
    merged = resolve_shape_rules({"require_subject": True})
    rule = merged["requirement"]
    assert rule["require_subject"] is True
    # Overrides merge in, they don't replace the whole rule — a caller who
    # only wants require_subject changed shouldn't lose require_any_of.
    assert rule["require_any_of"] == BARE_CLAIM_RULES["requirement"]["require_any_of"]


def test_shape_profile_exposes_requirement_rules_attribute():
    profile = shape_profile({"require_subject": True})
    assert profile.requirement_rules["requirement"]["require_subject"] is True
