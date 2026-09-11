"""build_gap_report — the claims-vs-census diff.

Census calls are monkeypatched: this tests the diff logic (which probable
instances live in chunks no claim touched) and the cost gate, not the census
mechanism itself (already covered by the ported test_census.py).
"""

from types import SimpleNamespace

from claimvalidator.claim_shims import ResolvedClaim
from claimvalidator.gap_report import _content_tokens, _stem, build_gap_report
from phases.census import CensusResult, CensusSpread
from phases.phase1_models import slugify


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


# ---------------------------------------------------------------- stemming
#
# Found via a real diagnostic run against a live document: "Buttons" vs
# census_many's own "element with role button that closes the dialog"
# scored 0.000 overlap under exact word matching — "buttons" and "button"
# share no tokens without this.

def test_plural_and_singular_share_a_stem():
    assert _stem("buttons") == _stem("button") == "button"


def test_short_words_are_never_stemmed():
    """The length-4 guard exists so real short words (not plurals of
    anything) survive untouched."""
    assert _stem("role") == "role"
    assert _stem("was") == "was"


def test_double_s_words_are_not_treated_as_plurals():
    """class/address/process end in "s" but are not "clas"/"addres"/
    "proces" pluralized — the naive strip-trailing-s rule would mangle
    them without this guard."""
    assert _stem("class") == "class"
    assert _stem("address") == "address"
    assert _stem("process") == "process"


def test_a_bare_plural_still_falls_short_of_a_long_descriptive_phrase():
    """The stemming fix closes the exact-token gap (0.000 -> real
    overlap) but doesn't manufacture a match where the phrases are
    genuinely different in scope — a single generic word against a long,
    specific phrase correctly stays below the match threshold. Real
    number from the diagnostic run this fix was based on: 0.2."""
    buttons = _content_tokens("Buttons")
    phrase = _content_tokens("element with role button that closes the dialog")
    overlap = len(buttons & phrase) / len(buttons | phrase)
    assert 0.0 < overlap < 0.5


# ---------------------------------------------------------------- issue #2: name reconciliation fallback
#
# _best_fuzzy_match (tested above) is left completely unchanged by this fix
# — every test above still passes unmodified. These tests cover the new
# third tier: for whatever _best_fuzzy_match still can't place,
# build_gap_report now falls back to phases.name_reconciliation.
# reconcile_names, the same LLM-judged mechanism phase1b_validation.py
# already runs in production for a different reconciliation job.

import json

from tests.conftest import FakeLLMClient


def _judge_response(pairs):
    return json.dumps([{"b": b, "a": a, "confident": c} for b, a, c in pairs])


def test_a_near_zero_overlap_paraphrase_resolves_via_the_reconciliation_fallback(monkeypatch):
    # The real diagnostic case from _best_fuzzy_match's own docstring: these
    # two names score 0.286 by Jaccard — well under the 0.5 free-tier
    # threshold — so the free tier alone leaves this a false gap.
    a = "ASCII alphanumerics and hyphens in build metadata"
    b = "Build metadata identifiers composition"
    spread = CensusSpread(concept="semver_rule", counts=[1], seen_in={slugify(a): 1},
                           display={slugify(a): a}, runs=1)
    result = CensusResult(concept="semver_rule", names=[b], chunk_of={slugify(b): 12})
    monkeypatch.setattr("claimvalidator.gap_report.census_repeated",
                         lambda *a_, **k: {"semver_rule": spread})
    monkeypatch.setattr("claimvalidator.gap_report.census_many",
                         lambda *a_, **k: {"semver_rule": result})

    # census_names (LIST B, what's being placed) is `residual` = [a];
    # sampled_names (LIST A, what it's matched against) is `result_names` =
    # [b] — so the judge response's "b" field is `a` and "a" field is `b`.
    client = FakeLLMClient([_judge_response([(a, b, True)])])
    report = build_gap_report(_ontology([("semver_rule", "d")]), ["c"] * 20,
                               llm_client=client, claims=[])

    gap = report.per_concept["semver_rule"]
    assert gap.never_addressed == [a]  # located, but no claim cited chunk 12
    assert "chunk 12" in gap.never_addressed_reasons[a]
    assert client.call_count == 1


def test_the_free_tier_resolving_a_name_never_reaches_the_llm_fallback(monkeypatch):
    # A name _best_fuzzy_match already resolves for free must not also pay
    # for a reconciliation call — this fallback is for the residual only.
    spread = CensusSpread(
        concept="keyboard_interaction", counts=[3], seen_in={"escape-key-closes-dialog": 3},
        display={"escape-key-closes-dialog": "Escape key closes dialog"}, runs=3,
    )
    result = CensusResult(
        concept="keyboard_interaction", names=["Pressing Escape closes the dialog"],
        chunk_of={"pressing-escape-closes-the-dialog": 7},
    )
    monkeypatch.setattr("claimvalidator.gap_report.census_repeated",
                         lambda *a, **k: {"keyboard_interaction": spread})
    monkeypatch.setattr("claimvalidator.gap_report.census_many",
                         lambda *a, **k: {"keyboard_interaction": result})

    client = FakeLLMClient([])  # any call at all raises — proves none happened
    claims = [ResolvedClaim(id="C1", text="pressing escape closes the dialog", source_chunks=[7])]
    report = build_gap_report(_ontology([("keyboard_interaction", "d")]), ["c"] * 10,
                               llm_client=client, claims=claims)

    assert report.per_concept["keyboard_interaction"].addressed_count == 1
    assert client.call_count == 0


def test_a_genuinely_unrelated_pair_still_reports_no_verified_citation(monkeypatch):
    # The judge can say "not a match" too — the fallback must not force one.
    spread = CensusSpread(concept="widget", counts=[1], seen_in={"foo": 1}, display={"foo": "Foo"}, runs=1)
    result = CensusResult(concept="widget", names=["Bar"], chunk_of={"bar": 3})
    monkeypatch.setattr("claimvalidator.gap_report.census_repeated",
                         lambda *a, **k: {"widget": spread})
    monkeypatch.setattr("claimvalidator.gap_report.census_many",
                         lambda *a, **k: {"widget": result})

    client = FakeLLMClient([_judge_response([("Bar", None, True)])])
    report = build_gap_report(_ontology([("widget", "d")]), ["c"] * 10,
                               llm_client=client, claims=[])

    reason = report.per_concept["widget"].never_addressed_reasons["Foo"]
    assert "no verified citation" in reason


def test_an_ambiguous_pairing_gets_its_own_reason_not_lumped_with_no_citation(monkeypatch):
    spread = CensusSpread(concept="api_operation", counts=[2], seen_in={"a": 1, "b": 1},
                           display={"a": "list_orders", "b": "create_orders"}, runs=1)
    result = CensusResult(concept="api_operation", names=["POST /orders/bulk"],
                           chunk_of={"post-orders-bulk": 5})
    monkeypatch.setattr("claimvalidator.gap_report.census_repeated",
                         lambda *a, **k: {"api_operation": spread})
    monkeypatch.setattr("claimvalidator.gap_report.census_many",
                         lambda *a, **k: {"api_operation": result})

    # The judge genuinely can't tell which of the two this refers to.
    client = FakeLLMClient([_judge_response([("list_orders", "POST /orders/bulk", False)])])
    report = build_gap_report(_ontology([("api_operation", "d")]), ["c"] * 10,
                               llm_client=client, claims=[])

    gap = report.per_concept["api_operation"]
    assert "list_orders" in gap.never_addressed
    reason = gap.never_addressed_reasons["list_orders"]
    assert "ambiguous" in reason
    assert "no verified citation" not in reason


def test_a_broken_llm_client_degrades_to_todays_behaviour_not_a_crash(monkeypatch):
    # Exercises reconcile_names'/_judge's OWN internal per-batch try/except
    # (phases/name_reconciliation.py) — a failed model call there is
    # swallowed and the affected names simply end up unmatched, never
    # raised. See the next test for gap_report.py's own, independent
    # try/except around the whole reconcile_names call.
    a = "ASCII alphanumerics and hyphens in build metadata"
    b = "Build metadata identifiers composition"
    spread = CensusSpread(concept="semver_rule", counts=[1], seen_in={slugify(a): 1},
                           display={slugify(a): a}, runs=1)
    result = CensusResult(concept="semver_rule", names=[b], chunk_of={slugify(b): 12})
    monkeypatch.setattr("claimvalidator.gap_report.census_repeated",
                         lambda *a_, **k: {"semver_rule": spread})
    monkeypatch.setattr("claimvalidator.gap_report.census_many",
                         lambda *a_, **k: {"semver_rule": result})

    class BrokenClient:
        def generate(self, prompt, system_prompt=None):
            raise RuntimeError("simulated model failure")

    # Must not raise — the residual name just stays unresolved, exactly as
    # it would have before this fallback existed.
    report = build_gap_report(_ontology([("semver_rule", "d")]), ["c"] * 20,
                               llm_client=BrokenClient(), claims=[])

    reason = report.per_concept["semver_rule"].never_addressed_reasons[a]
    assert "no verified citation" in reason


def test_reconcile_names_itself_raising_still_degrades_not_crashes(monkeypatch):
    # Belt and suspenders: even if reconcile_names raised OUTSIDE its own
    # internal per-batch guard (a bug in the module itself, not a model
    # failure), gap_report.py's own try/except around the call must still
    # catch it — this fallback existing must never make the gap report
    # itself less reliable than it was before.
    a = "ASCII alphanumerics and hyphens in build metadata"
    b = "Build metadata identifiers composition"
    spread = CensusSpread(concept="semver_rule", counts=[1], seen_in={slugify(a): 1},
                           display={slugify(a): a}, runs=1)
    result = CensusResult(concept="semver_rule", names=[b], chunk_of={slugify(b): 12})
    monkeypatch.setattr("claimvalidator.gap_report.census_repeated",
                         lambda *a_, **k: {"semver_rule": spread})
    monkeypatch.setattr("claimvalidator.gap_report.census_many",
                         lambda *a_, **k: {"semver_rule": result})

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated bug inside reconcile_names itself")

    monkeypatch.setattr("claimvalidator.gap_report.reconcile_names", _boom)

    report = build_gap_report(_ontology([("semver_rule", "d")]), ["c"] * 20,
                               llm_client=object(), claims=[])

    reason = report.per_concept["semver_rule"].never_addressed_reasons[a]
    assert "no verified citation" in reason
