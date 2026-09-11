"""`_build_prompt`'s rendered text — the part that can be checked without a
real model. Behaviour against a real model (does the judge actually reason
correctly about a threshold) can only be proven live; see issue #4 and the
scratchpad verification this fix was made against. This test only proves the
guidance text is present and reaches the model, not that it works.
"""
from phases.entailment import _build_prompt


class _FakeRequirement:
    def __init__(self, id, title):
        self.id = id
        self.title = title
        self.expected_behavior = ""
        self.criteria = []


def _prompt_for(claim_text: str) -> str:
    requirement = _FakeRequirement("C1", claim_text)
    return _build_prompt([(requirement, [(0, "some passage text")])])


def test_prompt_includes_threshold_and_range_guidance():
    prompt = _build_prompt([])
    assert "CONSISTENT WITH" in prompt
    assert "threshold" in prompt.lower()


def test_prompt_distinguishes_a_threshold_from_a_fixed_value():
    prompt = _build_prompt([])
    # The example pair this guidance exists to fix (see issue #4): a value on
    # the correct side of a stated threshold is not the same case as a value
    # that just differs from a document's own fixed number.
    assert "fails above 0.1%" in prompt
    assert "limit is 25 per second" in prompt


def test_the_original_different_value_rule_is_scoped_not_deleted():
    # The pre-existing "different value -> contradicts" instruction is still
    # here (it correctly catches real fixed-value conflicts elsewhere in this
    # session's runs) — this fix narrows it, it does not remove it.
    prompt = _build_prompt([])
    assert "FIXED value for the" in prompt


def test_claim_text_still_reaches_the_prompt_body():
    prompt = _prompt_for("A 3% difference fails the page.")
    assert "A 3% difference fails the page." in prompt
