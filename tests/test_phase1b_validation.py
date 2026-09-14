"""run_census's per-concept capture_range — census_repeated and
reconcile_across_concepts are monkeypatched, matching the convention
already used for gap_report's tests.
"""

from types import SimpleNamespace

from phases.census import CensusSpread
from phases.name_reconciliation import CrossReconciliation
from phases.phase1b_validation import run_census


class _Tracker:
    def step_start(self, *a, **k):
        pass

    def step_complete(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _ontology_with_one_instance(concept_name):
    instance = SimpleNamespace(name="thing1")
    concept = SimpleNamespace(name=concept_name, description="d", instances=[instance])
    return SimpleNamespace(concept_types=[concept])


def test_a_low_estimate_of_zero_does_not_crash_capture_range(monkeypatch):
    # A census that saw the concept 0 times in one run and 2 in the other two
    # gives low=0, high=2 -- a real, valid spread (see CensusSpread.low/.high),
    # not a malformed one. The reconciled branch divided by spread.low
    # unconditionally (phase1b_validation.py) while the unreconciled path
    # (CensusSpread.capture_range) already guarded the same division --
    # reproduces the ZeroDivisionError that guard was missing.
    spread = CensusSpread(concept="widget", counts=[0, 2, 2], runs=3)
    crossed = CrossReconciliation(concept="widget", matched={"thing1": ("thing1", "widget")})

    monkeypatch.setattr("phases.census.census_repeated", lambda *a, **k: {"widget": spread})
    monkeypatch.setattr(
        "phases.name_reconciliation.reconcile_across_concepts",
        lambda *a, **k: {"widget": crossed},
    )

    ontology = _ontology_with_one_instance("widget")
    rag_index = SimpleNamespace(chunks=["chunk one", "chunk two", "chunk three"])
    settings = {"census_on_completion": True, "census_max_chunks": 100, "census_runs": 3}

    result = run_census(ontology, rag_index, llm_client=None, tracker=_Tracker(), settings=settings)

    entry = result["per_concept"]["widget"]
    # found=1 against low=0: the low-bound ratio is undefined by division, but
    # any nonzero find against a lower bound of zero is full capture on that
    # bound, matching CensusSpread.capture_range's own existing convention.
    assert entry["capture_range"] == (0.5, 1.0)
