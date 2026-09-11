"""claimvalidator/numeric_threshold_check.py — pure functions, no LLM. See
issue #4: the entailment judge's own reasoning can compute a numeric
threshold comparison correctly and then override itself. These tests prove
the deterministic version doesn't have that failure mode, and is narrow
enough to abstain rather than guess everywhere it can't be sure.
"""
from claimvalidator.numeric_threshold_check import (
    _extract_claim_value_outcome,
    _extract_threshold_rules,
    _parse_number,
    check_numeric_consistency,
)

# The real brief sentences this module exists to get right, byte-for-byte
# from tools/openspec-adapter/examples/ui-testing/source-brief.md — bold
# markers included on purpose (see test_markdown_bold_between_operator_and_
# number_does_not_break_extraction below for exactly why that matters).
_THRESHOLD_PASSAGE = (
    "A page **fails** when more than **0.1% of its pixels** differ from the "
    "baseline. Anything at or below that is a pass."
)


# ---- number parsing --------------------------------------------------------

def test_leading_zero_variants_all_parse_to_the_same_float():
    assert _parse_number("0.1") == _parse_number(".1") == _parse_number("00.1") == 0.1


def test_thousands_separator_is_stripped():
    assert _parse_number("1,000") == 1000.0


def test_bare_integer_parses():
    assert _parse_number("3") == 3.0


# ---- threshold-rule extraction ----------------------------------------------

def test_markdown_bold_between_operator_and_number_does_not_break_extraction():
    # Found live: the real brief bolds exactly the words this module looks
    # for ("fails when more than **0.1% of its pixels**"), and the bold
    # markers sat directly between the operator phrase and its number,
    # breaking the original adjacency match and silently dropping the rule
    # (issue #4 follow-up — a claim that should have been caught wasn't,
    # because the deterministic check itself abstained without noticing).
    passage = (
        "A page **fails** when more than **0.1% of its pixels** differ "
        "from the baseline. Anything at or below that is a pass."
    )
    rules = _extract_threshold_rules(passage)
    assert len(rules) == 2
    above = next(r for r in rules if r.comparator == ">")
    assert above.value == 0.1 and above.outcome == "negative"


def test_a_markdown_heading_with_no_terminal_punctuation_does_not_poison_the_next_sentence():
    # Found live: "## Pass and fail\n\nA page fails when more than 0.1%..."
    # has no period after the heading, so without a heading boundary this
    # becomes one "sentence" carrying THREE outcome words ("Pass", "fail"
    # from the heading text itself, "fails" from the actual rule) -- which
    # this module's own ambiguity guard then correctly refused to touch,
    # on text that should never have been treated as one sentence.
    passage = "## Pass and fail\n\nA page fails when more than 0.1% of its pixels differ from the baseline."
    rules = _extract_threshold_rules(passage)
    assert len(rules) == 1
    assert rules[0].comparator == ">" and rules[0].value == 0.1 and rules[0].outcome == "negative"


def test_two_overlapping_passages_repeating_the_same_rule_still_resolves():
    # Retrieved passages commonly overlap at chunk boundaries -- the same
    # sentence can come back in two passages verbatim. Two AGREEING rules
    # must not be mistaken for two CONFLICTING ones.
    claim = "A 3% difference fails the page."
    passages = [_THRESHOLD_PASSAGE, _THRESHOLD_PASSAGE]  # duplicated on purpose
    result = check_numeric_consistency(claim, passages)
    assert result is not None and result.verdict == "entails"


def test_the_real_brief_sentence_pair_yields_two_opposite_rules():
    rules = _extract_threshold_rules(_THRESHOLD_PASSAGE)
    assert len(rules) == 2
    above = next(r for r in rules if r.comparator == ">")
    at_or_below = next(r for r in rules if r.comparator == "<=")
    assert above.value == 0.1 and above.outcome == "negative"
    assert at_or_below.value == 0.1 and at_or_below.outcome == "positive"


def test_a_number_with_no_outcome_word_yields_no_rule():
    # The real api-testing brief's rate limit — a plain fixed-value fact,
    # not a pass/fail branch. Must never be mistaken for a threshold rule.
    assert _extract_threshold_rules("The verifier sends no more than 10 requests per second.") == []


def test_a_bare_operator_without_a_recognized_outcome_word_yields_no_rule():
    assert _extract_threshold_rules("The gate captures at most 10 screenshots per run.") == []


def test_no_more_than_is_not_mistaken_for_more_than():
    rules = _extract_threshold_rules("The request is rejected if there are no more than 3 retries left.")
    assert len(rules) == 1
    assert rules[0].comparator == "<="


# ---- claim value+outcome extraction -----------------------------------------

def test_claim_with_one_number_and_one_outcome_word_extracts_both():
    assert _extract_claim_value_outcome("A 3% difference fails the page.") == (3.0, "negative")
    assert _extract_claim_value_outcome("A 0.5% difference passes the page.") == (0.5, "positive")


def test_claim_with_two_numbers_abstains():
    # Two numbers reads as a likely compound claim (issue #3's territory),
    # not this module's — guessing which one the outcome word refers to
    # would be exactly the kind of guess this module exists to avoid.
    assert _extract_claim_value_outcome("A 3% or 5% difference fails the page.") is None


def test_a_flag_being_passed_is_not_mistaken_for_an_outcome_word():
    # Found live: this module wrongly overrode an already-correct "entails"
    # to "contradicts" on this exact claim. "was passed" here means a CLI
    # flag was supplied, not that a check passed — "passed" is genuinely
    # ambiguous between those two senses and was removed from the
    # vocabulary for exactly this reason.
    claim = (
        "When a page differs from its baseline by 2% or more, leave the "
        "baseline unchanged unless `--update-baselines` was passed."
    )
    assert _extract_claim_value_outcome(claim) is None


def test_claim_with_no_recognized_outcome_word_abstains():
    assert _extract_claim_value_outcome("The gate captures at 2x device pixel ratio.") is None


def test_spelled_out_numbers_are_not_recognized():
    # "retry up to three times" - "three" is a word, not a digit, so this
    # never becomes a candidate at all. Left entirely to the judge, as today.
    assert _extract_claim_value_outcome("Retry a failed request up to three times.") is None


# ---- end-to-end: the real cases this module exists for ----------------------

def test_the_consistent_case_is_entails():
    # 3% is on the failing side of "more than 0.1%" -> consistent with the
    # claim's own stated "fails" outcome.
    result = check_numeric_consistency("A 3% difference fails the page.", [_THRESHOLD_PASSAGE])
    assert result is not None
    assert result.verdict == "entails"


def test_the_contradictory_case_is_contradicts():
    # 0.5% is also on the failing side, but the claim says it passes.
    result = check_numeric_consistency("A 0.5% difference passes the page.", [_THRESHOLD_PASSAGE])
    assert result is not None
    assert result.verdict == "contradicts"


def test_below_threshold_direction_resolves_via_the_anaphoric_companion_rule():
    # A value below the threshold only resolves correctly if the second
    # sentence's "at or below THAT is a pass" is actually recovered as its
    # own rule (0.1, positive) - proving the anaphoric carry-forward works,
    # not just that the single explicit ">" rule happens to cover the two
    # cases above.
    result = check_numeric_consistency("A 0.05% difference passes the page.", [_THRESHOLD_PASSAGE])
    assert result is not None and result.verdict == "entails"

    result = check_numeric_consistency("A 0.05% difference fails the page.", [_THRESHOLD_PASSAGE])
    assert result is not None and result.verdict == "contradicts"


def test_adapter_generated_phrasing_works_too():
    # The real claim text the adapter actually produces, "When X, Y." form.
    result = check_numeric_consistency(
        "When 3% of a page's pixels differ from its baseline, fail the page.",
        [_THRESHOLD_PASSAGE],
    )
    assert result is not None and result.verdict == "entails"

    result = check_numeric_consistency(
        "When 0.5% of a page's pixels differ from its baseline, pass the page.",
        [_THRESHOLD_PASSAGE],
    )
    assert result is not None and result.verdict == "contradicts"


# ---- abstention on everything this module is not meant to touch ------------

def test_a_real_fixed_value_conflict_is_left_to_the_judge():
    # The api-testing rate-limit claim: no outcome word on either side, so
    # this module has nothing to say - the LLM's own (already correct)
    # "contradicts" for a literal 25-vs-10 mismatch stands untouched.
    claim = "The verifier SHALL send no more than 25 requests per second to the target service."
    passage = "The verifier sends no more than 10 requests per second to the target service."
    assert check_numeric_consistency(claim, [passage]) is None


def test_a_spelled_out_retry_count_claim_is_left_to_the_judge():
    claim = "When a request receives an HTTP 503 response, retry the request up to three attempts."
    passage = "On a connection error or an HTTP 503, it retries the request once."
    assert check_numeric_consistency(claim, [passage]) is None


def test_a_device_pixel_ratio_claim_is_left_to_the_judge():
    claim = "The gate SHALL capture every page at 2x device pixel ratio for retina fidelity."
    passage = "All captures are taken at 1x device pixel ratio. Retina/2x rendering is out of scope for v1."
    assert check_numeric_consistency(claim, [passage]) is None


def test_no_threshold_rule_anywhere_abstains():
    assert check_numeric_consistency("A 3% difference fails the page.", ["Nothing relevant here."]) is None
