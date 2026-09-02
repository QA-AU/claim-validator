"""build_gap_report — the claims-vs-census diff.

Census calls are monkeypatched: this tests the diff logic (which probable
instances live in chunks no claim touched) and the cost gate, not the census
mechanism itself (already covered by the ported test_census.py).
"""

from types import SimpleNamespace

from claimvalidator.claim_shims import ResolvedClaim
from claimvalidator.gap_report import build_gap_report
from phases.census import CensusResult, CensusSpread


def _ontology(names_and_descriptions):
    concept_types = [SimpleNamespace(name=n, description=d) for n, d in names_and_descriptions]
    return SimpleNamespace(concept_types=concept_types, domain="test")


def test_claim_touching_a_chunk_marks_that_instance_addressed(monkeypatch):
    spread = CensusSpread(
        concept="grant_type",
        counts=[4, 4, 4],
        seen_in={"authorization_code": 3, "client_credentials": 3},
        display={"authorization_code": "authorization_code", "client_credentials": "client_credentials"},
        runs=3,
    )
    result = CensusResult(
        concept="grant_type",
        names=["authorization_code", "client_credentials"],
        # Keyed by slug (hyphens), matching census.py's own convention —
        # slugify("authorization_code") == "authorization-code".
        chunk_of={"authorization-code": 24, "client-credentials": 140},
    )

    monkeypatch.setattr("claimvalidator.gap_report.census_repeated",
                         lambda *a, **k: {"grant_type": spread})
    monkeypatch.setattr("claimvalidator.gap_report.census_many",
                         lambda *a, **k: {"grant_type": result})

    claims = [ResolvedClaim(id="C1", text="about client credentials", source_chunks=[140])]
    report = build_gap_report(_ontology([("grant_type", "d")]), ["c"] * 5, llm_client=None,
                               claims=claims)

    assert report.ran is True
    gap = report.per_concept["grant_type"]
    assert gap.addressed_count == 1
    assert gap.never_addressed == ["authorization_code"]


def test_no_claims_touch_anything_everything_is_a_gap(monkeypatch):
    spread = CensusSpread(concept="error", counts=[2, 2],
                           seen_in={"invalid_request": 2, "access_denied": 2},
                           display={"invalid_request": "invalid_request", "access_denied": "access_denied"},
                           runs=2)
    result = CensusResult(concept="error", names=["invalid_request", "access_denied"],
                           chunk_of={"invalid_request": 5, "access_denied": 6})

    monkeypatch.setattr("claimvalidator.gap_report.census_repeated",
                         lambda *a, **k: {"error": spread})
    monkeypatch.setattr("claimvalidator.gap_report.census_many",
                         lambda *a, **k: {"error": result})

    report = build_gap_report(_ontology([("error", "d")]), ["c"] * 5, llm_client=None, claims=[])

    gap = report.per_concept["error"]
    assert gap.addressed_count == 0
    assert set(gap.never_addressed) == {"invalid_request", "access_denied"}


def test_over_the_chunk_limit_is_skipped_unless_forced(monkeypatch):
    called = {"count": 0}
    monkeypatch.setattr("claimvalidator.gap_report.census_repeated",
                         lambda *a, **k: called.__setitem__("count", called["count"] + 1) or {})
    monkeypatch.setattr("claimvalidator.gap_report.census_many", lambda *a, **k: {})

    report = build_gap_report(_ontology([]), ["c"] * 500, llm_client=None, claims=[],
                               max_chunks=200, force=False)
    assert report.ran is False
    assert "200" in report.skipped_reason
    assert called["count"] == 0  # never actually paid for the census


def test_force_runs_it_anyway_over_the_limit(monkeypatch):
    monkeypatch.setattr("claimvalidator.gap_report.census_repeated", lambda *a, **k: {})
    monkeypatch.setattr("claimvalidator.gap_report.census_many", lambda *a, **k: {})

    report = build_gap_report(_ontology([]), ["c"] * 500, llm_client=None, claims=[],
                               max_chunks=200, force=True)
    assert report.ran is True


# ---------------------------------------------------------------- fuzzy name matching
#
# census_repeated's `probable` names and census_many's `chunk_of` names come
# from two independent LLM calls that each phrase the same real instance in
# their own words. Found live: a real gap report reported every
# keyboard-interaction instance as "no verified citation" even though the
# census plainly located them, because census_repeated said e.g. "Escape key
# closes dialog" while census_many's separate call said "Pressing Escape
# closes the dialog" — different exact strings, same fact.

def test_a_paraphrased_name_still_resolves_to_its_chunk(monkeypatch):
    spread = CensusSpread(
        concept="keyboard_interaction", counts=[3],
        seen_in={"escape-key-closes-dialog": 3},
        display={"escape-key-closes-dialog": "Escape key closes dialog"},
        runs=3,
    )
    # census_many's own, differently-phrased name for the same real instance
    # — not present under the exact slug census_repeated used.
    result = CensusResult(
        concept="keyboard_interaction",
        names=["Pressing Escape closes the dialog"],
        chunk_of={"pressing-escape-closes-the-dialog": 7},
    )
    monkeypatch.setattr("claimvalidator.gap_report.census_repeated",
                         lambda *a, **k: {"keyboard_interaction": spread})
    monkeypatch.setattr("claimvalidator.gap_report.census_many",
                         lambda *a, **k: {"keyboard_interaction": result})

    claims = [ResolvedClaim(id="C1", text="pressing escape closes the dialog", source_chunks=[7])]
    report = build_gap_report(_ontology([("keyboard_interaction", "d")]), ["c"] * 10,
                               llm_client=None, claims=claims)

    gap = report.per_concept["keyboard_interaction"]
    assert gap.addressed_count == 1
    assert gap.never_addressed == []


def test_a_paraphrased_name_with_no_claim_citation_reports_the_real_chunk(monkeypatch):
    spread = CensusSpread(
        concept="keyboard_interaction", counts=[3],
        seen_in={"escape-key-closes-dialog": 3},
        display={"escape-key-closes-dialog": "Escape key closes dialog"},
        runs=3,
    )
    result = CensusResult(
        concept="keyboard_interaction",
        names=["Pressing Escape closes the dialog"],
        chunk_of={"pressing-escape-closes-the-dialog": 7},
    )
    monkeypatch.setattr("claimvalidator.gap_report.census_repeated",
                         lambda *a, **k: {"keyboard_interaction": spread})
    monkeypatch.setattr("claimvalidator.gap_report.census_many",
                         lambda *a, **k: {"keyboard_interaction": result})

    report = build_gap_report(_ontology([("keyboard_interaction", "d")]), ["c"] * 10,
                               llm_client=None, claims=[])

    gap = report.per_concept["keyboard_interaction"]
    assert gap.never_addressed == ["Escape key closes dialog"]
    reason = gap.never_addressed_reasons["Escape key closes dialog"]
    assert "chunk 7" in reason
    assert "no verified citation" not in reason


def test_unrelated_names_do_not_fuzzy_match(monkeypatch):
    """Guard against the fix itself becoming a false-positive source: two
    genuinely different instances must not be treated as the same one just
    because they happen to share a stray word."""
    spread = CensusSpread(
        concept="keyboard_interaction", counts=[3],
        seen_in={"escape-key-closes-dialog": 3},
        display={"escape-key-closes-dialog": "Escape key closes dialog"},
        runs=3,
    )
    result = CensusResult(
        concept="keyboard_interaction",
        names=["Tab key moves focus to the next element"],
        chunk_of={"tab-key-moves-focus-to-the-next-element": 3},
    )
    monkeypatch.setattr("claimvalidator.gap_report.census_repeated",
                         lambda *a, **k: {"keyboard_interaction": spread})
    monkeypatch.setattr("claimvalidator.gap_report.census_many",
                         lambda *a, **k: {"keyboard_interaction": result})

    report = build_gap_report(_ontology([("keyboard_interaction", "d")]), ["c"] * 10,
                               llm_client=None, claims=[])

    gap = report.per_concept["keyboard_interaction"]
    reason = gap.never_addressed_reasons["Escape key closes dialog"]
    assert "no verified citation" in reason
