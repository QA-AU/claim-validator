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


def test_prompt_includes_the_threshold_reasoning_procedure():
    prompt = _build_prompt([])
    assert "THRESHOLD or boundary rule" in prompt
    assert "work through these steps" in prompt.lower()


def test_prompt_worked_example_spells_out_the_correct_answer_for_both_directions():
    prompt = _build_prompt([])
    # The exact pair this fix exists for (issue #4): a value on the failing
    # side of a stated threshold is "entails" when the claim also says fail,
    # and "contradicts" when the claim says pass — spelled out, not left for
    # the model to derive from a general rule.
    assert '"a 3% difference fails the page"' in prompt
    assert '"a 0.5% difference passes the page"' in prompt
    assert "SAME" in prompt and "not a contradiction -> entails" in prompt
    assert "DIFFERENT outcome -> contradicts" in prompt
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


def test_prompt_warns_against_treating_a_self_referential_comment_as_evidence():
    # Issue #17: the judge accepted "prevents race conditions" as entailed
    # because the code carried a comment asserting the same thing, rather
    # than checking whether the SQL actually delivers that property.
    prompt = _build_prompt([])
    assert "merely\n                     ASSERTS the same conclusion" in prompt
    assert "prevents race conditions" in prompt
    assert "is not itself\n                     sufficient evidence" in prompt
    assert "a lock, a transaction, or a\n                     conditional check" in prompt
