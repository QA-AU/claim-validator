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
    def __init__(self, retrieval: Retrieval):
        self.retrieval = retrieval
        self.calls: list[list[str]] = []

    def retrieve_union(self, probes, top_k=5):
        self.calls.append(list(probes))
        return self.retrieval


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
