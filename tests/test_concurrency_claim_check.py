"""claimvalidator/concurrency_claim_check.py — pure functions, no LLM. See
issue #17: the judge accepted a claim that a batched SQL update "prevents
race conditions" because the code's own comment asserted the same thing,
never checking whether the SQL actually shows a synchronization mechanism.
These tests prove the deterministic version doesn't have that failure mode,
and abstains (returns None) on anything outside its narrow, stated shape.
"""
from claimvalidator.concurrency_claim_check import (
    VERDICT_MENTIONS_ONLY,
    check_concurrency_consistency,
)

# The real passage from docs/refactor-accuracy-demo/refactored-checkout.js
# that produced the live miss this module exists to catch.
_UNSAFE_BATCH_UPDATE = (
    "// 5. Execute all stock updates in a single batch query "
    "(prevents race conditions)\n"
    "UPDATE products SET stock = CASE id WHEN $1 THEN $2 END "
    "WHERE id = ANY($3)"
)

_CLAIM = (
    "The refactored code implements atomic batch stock updates using a "
    "SQL CASE statement to prevent race conditions where concurrent "
    "checkouts could oversell inventory."
)


def test_the_real_issue_17_case_overrides_to_mentions_only():
    override = check_concurrency_consistency(_CLAIM, [_UNSAFE_BATCH_UPDATE])
    assert override is not None
    assert override.verdict == VERDICT_MENTIONS_ONLY
    assert "synchronization mechanism" in override.explanation


def test_a_transaction_in_the_passage_means_no_override():
    passage = (
        "BEGIN;\n"
        "UPDATE products SET stock = CASE id WHEN $1 THEN $2 END "
        "WHERE id = ANY($3);\n"
        "COMMIT;"
    )
    assert check_concurrency_consistency(_CLAIM, [passage]) is None


def test_a_row_lock_in_the_passage_means_no_override():
    passage = "SELECT * FROM products WHERE id = $1 FOR UPDATE"
    assert check_concurrency_consistency(_CLAIM, [passage]) is None


def test_a_guarded_conditional_update_means_no_override():
    passage = "UPDATE products SET stock = stock - $1 WHERE id = $2 AND stock >= $1"
    assert check_concurrency_consistency(_CLAIM, [passage]) is None


def test_a_claim_without_a_trigger_phrase_is_untouched_regardless_of_passage():
    claim = "The refactored code fetches all products in a single query."
    assert check_concurrency_consistency(claim, [_UNSAFE_BATCH_UPDATE]) is None


def test_thread_safe_phrasing_also_triggers():
    claim = "The counter increment is thread-safe."
    assert check_concurrency_consistency(claim, ["counter += 1  # thread-safe"]) is not None


def test_no_passages_at_all_still_overrides_since_nothing_confirms_the_property():
    assert check_concurrency_consistency(_CLAIM, []) is not None
