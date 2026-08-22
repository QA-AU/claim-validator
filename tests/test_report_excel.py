"""claimvalidator/report_excel.py's pure helper functions — the ones that
decide what a report cell actually says, independent of openpyxl. Found
worth testing directly after a report reader couldn't tell which claim IDs
a Quality tab metric like "contradicted: 2" was actually about.
"""

from claimvalidator.pipeline import ClaimResult
from claimvalidator.report_excel import _claim_ids_for


def _claim(id, **overrides):
    defaults = dict(
        id=id, text="t", shape_ok=True, shape_reason=None, verdict="entails",
        judged=True, agreement="3/3", cited_chunks=[0], reason="r",
    )
    defaults.update(overrides)
    return ClaimResult(**defaults)


def test_a_metric_with_no_claim_id_meaning_says_so_rather_than_blank():
    assert _claim_ids_for([_claim("C1")], "llm_calls") == "(whole-run figure, not per-claim)"


def test_a_zero_count_metric_reports_none_not_an_empty_string():
    claims = [_claim("C1", verdict="entails")]
    assert _claim_ids_for(claims, "contradicted") == "(none)"


def test_contradicted_names_only_the_contradicting_claims():
    claims = [
        _claim("C1", verdict="entails"),
        _claim("C2", verdict="contradicts"),
        _claim("C3", verdict="contradicts"),
    ]
    assert _claim_ids_for(claims, "contradicted") == "C2, C3"


def test_shape_violations_names_claims_that_failed_the_shape_check():
    claims = [_claim("C1", shape_ok=False), _claim("C2", shape_ok=True)]
    assert _claim_ids_for(claims, "shape_violations") == "C1"


def test_retrieval_found_nothing_names_claims_with_no_cited_chunks():
    claims = [_claim("C1", cited_chunks=[]), _claim("C2", cited_chunks=[3])]
    assert _claim_ids_for(claims, "retrieval_found_nothing") == "C1"


def test_undecided_names_judged_claims_the_judge_could_not_settle():
    claims = [
        _claim("C1", judged=True, decided=True),
        _claim("C2", judged=True, decided=False),
        _claim("C3", judged=False, decided=False),  # unjudged, not "undecided"
    ]
    assert _claim_ids_for(claims, "undecided") == "C2"


def test_overturned_names_only_escalated_claims_whose_verdict_actually_changed():
    claims = [
        _claim("C1", escalated=True, escalated_from="entails", verdict="entails"),  # confirmed, not overturned
        _claim("C2", escalated=True, escalated_from="mentions_only", verdict="contradicts"),
        _claim("C3", escalated=False),
    ]
    assert _claim_ids_for(claims, "overturned") == "C2"
