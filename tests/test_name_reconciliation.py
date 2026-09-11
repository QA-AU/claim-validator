"""phases/name_reconciliation.py::reconcile_names — no dedicated coverage
existed in this repo before issue #2 gave it a second real caller
(claimvalidator/gap_report.py, alongside its original caller in
phases/phase1b_validation.py). These tests exercise the three tiers
(exact, lexical, judged) and the two "don't guess" outcomes (unmatched,
ambiguous) directly, independent of the gap report.
"""
import json

from tests.conftest import FakeLLMClient

from phases.name_reconciliation import MATCH_EXACT, MATCH_JUDGED, MATCH_LEXICAL, reconcile_names


def _judge_response(pairs):
    """pairs: list of (b, a_or_None, confident)."""
    return json.dumps([{"b": b, "a": a, "confident": c} for b, a, c in pairs])


def test_identical_slugs_match_exactly_no_model_needed():
    result = reconcile_names(["GET /orders"], ["GET /orders"], llm_client=None)
    assert result.matched == {"GET /orders": "GET /orders"}
    assert result.method["GET /orders"] == MATCH_EXACT
    assert result.calls_made == 0


def test_a_verbose_restatement_matches_lexically_no_model_needed():
    # The module's own docstring example: one pass is more verbose than the
    # other but says everything the shorter name does.
    result = reconcile_names(
        ["Authorization Bearer Token"],
        ["Authorization: Bearer <token> header"],
        llm_client=None,
    )
    assert result.matched == {
        "Authorization: Bearer <token> header": "Authorization Bearer Token"
    }
    assert result.method["Authorization: Bearer <token> header"] == MATCH_LEXICAL
    assert result.calls_made == 0


def test_a_domain_paraphrase_with_no_shared_words_is_settled_by_the_judge():
    # The module's own docstring example: "list_orders" vs "GET /orders" —
    # needs domain knowledge, not string similarity; no threshold separates
    # it from "create_orders".
    client = FakeLLMClient([_judge_response([("GET /orders", "list_orders", True)])])
    result = reconcile_names(
        ["list_orders"], ["GET /orders"], concept="api_operation",
        llm_client=client,
    )
    assert result.matched == {"GET /orders": "list_orders"}
    assert result.method["GET /orders"] == MATCH_JUDGED
    assert result.calls_made == 1


def test_judge_saying_no_match_leaves_it_unmatched_not_forced():
    client = FakeLLMClient([_judge_response([("DELETE /orders", None, True)])])
    result = reconcile_names(
        ["list_orders"], ["DELETE /orders"], concept="api_operation",
        llm_client=client,
    )
    assert result.matched == {}
    assert result.unmatched == ["DELETE /orders"]


def test_judge_uncertain_is_ambiguous_not_a_forced_match():
    client = FakeLLMClient([_judge_response([("PATCH /orders/{id}", "list_orders", False)])])
    result = reconcile_names(
        ["list_orders", "create_orders"], ["PATCH /orders/{id}"], concept="api_operation",
        llm_client=client,
    )
    assert result.matched == {}
    assert [c for c, _ in result.ambiguous] == ["PATCH /orders/{id}"]


def test_no_llm_client_skips_the_judged_tier_entirely_not_a_crash():
    # A name only the judge could settle stays unmatched, not ambiguous —
    # the judge never looked, so there's nothing to be unsure about.
    result = reconcile_names(["list_orders"], ["GET /orders"], llm_client=None)
    assert result.matched == {}
    assert result.unmatched == ["GET /orders"]
    assert result.calls_made == 0


def test_one_to_one_a_second_claim_on_a_taken_name_is_ambiguous():
    # The module's own stated rule: "GET /orders and DELETE /orders are both
    # list_orders" is a sign the alignment is wrong, not two matches.
    client = FakeLLMClient([_judge_response([
        ("GET /orders", "list_orders", True),
        ("DELETE /orders", "list_orders", True),
    ])])
    result = reconcile_names(
        ["list_orders"], ["GET /orders", "DELETE /orders"], concept="api_operation",
        llm_client=client,
    )
    assert len(result.matched) == 1
    # Whichever one claimed it first is matched; the second is ambiguous, not
    # silently dropped and not a second, contradictory match.
    matched_name = next(iter(result.matched))
    other = "DELETE /orders" if matched_name == "GET /orders" else "GET /orders"
    assert other in [c for c, _ in result.ambiguous]
