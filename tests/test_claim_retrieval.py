"""claim_retrieval.retrieve_for_claim — the retrieval-only half of ask.py.

Checks the one behavior worth locking down: concept surface_terms become a
SEPARATE probe, never concatenated onto the claim text (see the module
docstring for why that distinction matters), plus the two outcomes a caller
actually branches on — found something, found nothing.
"""

from types import SimpleNamespace

from claimvalidator.claim_retrieval import retrieve_for_claim
from phases.phase1_rag_indexer import Retrieval


class FakeSearcher:
    def __init__(self, retrieval: Retrieval, clause_retrievals: dict[str, Retrieval] = None):
        self.retrieval = retrieval
        # query -> Retrieval, for the per-clause .retrieve() calls issue #3
        # adds — separate from .retrieve_union() since real code never
        # calls them with the same probe shape (a full probe list vs. one
        # bare clause string).
        self.clause_retrievals = clause_retrievals or {}
        self.calls: list[list[str]] = []      # retrieve_union call args
        self.retrieve_calls: list[str] = []   # retrieve() call args, in order

    def retrieve_union(self, probes, top_k=5):
        self.calls.append(list(probes))
        return self.retrieval

    def retrieve(self, query, top_k=5):
        self.retrieve_calls.append(query)
        return self.clause_retrievals.get(query, Retrieval(context="", indices=[], scores=[]))


def _ontology(concept_types):
    return SimpleNamespace(concept_types=concept_types, domain="test")


def _concept(name, surface_terms):
    return SimpleNamespace(name=name, surface_terms=surface_terms, description="")


def test_found_something_returns_chunk_indices():
    searcher = FakeSearcher(Retrieval(context="x", indices=[3, 7], scores=[0.5, 0.3]))
    ontology = _ontology([])
    result = retrieve_for_claim("some claim", ontology, searcher, llm_client=None)
    assert result.chunk_indices == [3, 7]
    assert result.expansion_used is False


def test_found_nothing_returns_empty_not_fabricated():
    searcher = FakeSearcher(Retrieval(context="", indices=[9], scores=[0.0]))
    ontology = _ontology([])

    class ExpansionRefusingClient:
        def generate(self, prompt, system_prompt=None):
            return "[]"  # expand_query gets an empty terms list back

    result = retrieve_for_claim("nothing matches", ontology, searcher,
                                 llm_client=ExpansionRefusingClient())
    assert result.chunk_indices == []


def test_matching_concept_surface_terms_become_a_separate_probe():
    searcher = FakeSearcher(Retrieval(context="x", indices=[1], scores=[0.4]))
    ontology = _ontology([_concept("grant_type", ["Client Credentials", "Authorization Code"])])

    retrieve_for_claim("the client credentials grant type", ontology, searcher, llm_client=None)

    assert len(searcher.calls) == 1
    probes = searcher.calls[0]
    # Claim text is probe 0, unmodified — surface terms never get concatenated
    # onto it (that's the precision-losing pattern this module exists to avoid).
    assert probes[0] == "the client credentials grant type"
    assert len(probes) == 2
    assert "Client Credentials" in probes[1]


def test_non_matching_concept_contributes_no_second_probe():
    searcher = FakeSearcher(Retrieval(context="x", indices=[1], scores=[0.4]))
    ontology = _ontology([_concept("scope", ["requested scope", "invalid_scope"])])

    retrieve_for_claim("something about grant types", ontology, searcher, llm_client=None)

    assert len(searcher.calls[0]) == 1  # no concept matched, no second probe


# ---------------------------------------------------------------- issue #3: compound-claim widening

def test_a_non_splitting_claim_makes_no_extra_retrieve_calls():
    # The common case: no clause split, so retrieval is byte-for-byte what
    # it was before this fix existed.
    searcher = FakeSearcher(Retrieval(context="x", indices=[3, 7], scores=[0.5, 0.3]))
    ontology = _ontology([])

    result = retrieve_for_claim("Wash your hands for 20 seconds with soap.", ontology,
                                 searcher, llm_client=None)

    assert result.chunk_indices == [3, 7]
    assert result.widened_for_clauses is False
    assert searcher.retrieve_calls == []


def test_a_compound_claim_retrieves_separately_per_clause_and_unions():
    base = Retrieval(context="x", indices=[1], scores=[0.5])
    clause_a = "Use soap and water for at least 20 seconds"
    clause_b = "use a hand sanitizer with at least 60% alcohol if soap is unavailable"
    searcher = FakeSearcher(base, clause_retrievals={
        clause_a: Retrieval(context="a", indices=[1, 2], scores=[0.6, 0.4]),
        clause_b: Retrieval(context="b", indices=[9], scores=[0.7]),
    })
    ontology = _ontology([])

    claim = f"{clause_a}; and {clause_b}."
    result = retrieve_for_claim(claim, ontology, searcher, llm_client=None)

    assert result.widened_for_clauses is True
    assert result.chunk_indices == [1, 2, 9]  # base [1] unioned with both clauses, deduped+sorted
    assert searcher.retrieve_calls == [clause_a, clause_b]


def test_a_clause_that_finds_nothing_contributes_no_junk_chunks():
    # A weak/zero-score retrieval still returns *some* indices (argsort has
    # to return something) — found_nothing must gate those out, not just
    # the whole-claim probe's own found_nothing case.
    base = Retrieval(context="x", indices=[1], scores=[0.5])
    clause_a = "the gate waits for the network to be idle"
    clause_b = "it waits for web fonts to load"
    searcher = FakeSearcher(base, clause_retrievals={
        clause_a: Retrieval(context="a", indices=[1, 2], scores=[0.6, 0.4]),
        clause_b: Retrieval(context="junk", indices=[99], scores=[0.0]),  # found_nothing
    })
    ontology = _ontology([])

    claim = f"{clause_a}, and {clause_b}."
    result = retrieve_for_claim(claim, ontology, searcher, llm_client=None)

    assert 99 not in result.chunk_indices
    assert result.chunk_indices == [1, 2]
    assert result.widened_for_clauses is True  # clause_a still genuinely added chunk 2


def test_a_compound_claim_whose_clauses_add_nothing_new_is_not_marked_widened():
    # Splitting happened, but neither clause found anything the base
    # retrieval didn't already have — nothing to flag as "widened".
    base = Retrieval(context="x", indices=[1, 2], scores=[0.5, 0.4])
    clause_a = "the gate waits for the network to be idle"
    clause_b = "it waits for web fonts to load"
    searcher = FakeSearcher(base, clause_retrievals={
        clause_a: Retrieval(context="a", indices=[1], scores=[0.6]),
        clause_b: Retrieval(context="b", indices=[2], scores=[0.5]),
    })
    ontology = _ontology([])

    claim = f"{clause_a}, and {clause_b}."
    result = retrieve_for_claim(claim, ontology, searcher, llm_client=None)

    assert result.chunk_indices == [1, 2]
    assert result.widened_for_clauses is False


def test_the_original_diagnostic_case_issue_3_was_filed_over():
    # The CDC handwashing claim from issue #3's own live diagnosis: retrieval
    # found only one of the two supporting passages for a two-part claim.
    # The whole-claim probe finds only the soap-and-water passage (chunk 2);
    # per-clause retrieval recovers the sanitizer passage (chunk 9) too.
    base = Retrieval(context="x", indices=[2], scores=[0.55])
    clause_a = "wash hands with soap and water for at least 20 seconds"
    clause_b = "use a hand sanitizer that contains at least 60% alcohol if soap and water are not available"
    searcher = FakeSearcher(base, clause_retrievals={
        clause_a: Retrieval(context="a", indices=[2], scores=[0.6]),
        clause_b: Retrieval(context="b", indices=[9], scores=[0.5]),
    })
    ontology = _ontology([])

    claim = f"{clause_a}; and {clause_b}."
    result = retrieve_for_claim(claim, ontology, searcher, llm_client=None)

    assert 2 in result.chunk_indices and 9 in result.chunk_indices
    assert result.widened_for_clauses is True
