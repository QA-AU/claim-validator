"""Retrieval-only half of the source repo's `phases/ask.py::ask()`.

`ask()` does two things in one function: build retrieval probes and generate
a fresh LLM answer from what it finds. Here, only the first half is wanted —
`phases.entailment.judge_entailment` decides whether a passage supports an
existing claim; a second, answer-generating LLM call has no role in that.

The one detail worth keeping from `ask()` verbatim: ontology concept
surface_terms become a SEPARATE probe, never concatenated onto the claim
text. Concatenating them lets a few generic words (the concept's own surface
terms) outweigh the one distinctive word in the claim, which displaces the
right chunks instead of adding to them — see `retrieve_union`'s own
docstring in `phases/phase1_rag_indexer.py` for the measured case this
guards against.
"""

from dataclasses import dataclass, field
from typing import List

ASK_TOP_K = 5


@dataclass
class RetrievalResult:
    chunk_indices: List[int] = field(default_factory=list)
    expansion_used: bool = False


def retrieve_for_claim(claim_text: str, ontology, searcher, llm_client,
                        top_k: int = ASK_TOP_K) -> RetrievalResult:
    """Chunk indices that might support one claim, or none if nothing matches.

    Never fabricates a citation: an empty `chunk_indices` means retrieval
    genuinely found nothing, which the entailment judge already reports as
    `judged=False` rather than a false verdict — no extra handling needed
    here for that case.
    """
    lowered = claim_text.lower()
    probe_terms: List[str] = []
    for concept in ontology.concept_types:
        name_words = concept.name.replace("_", " ").lower()
        if name_words in lowered or any(t.lower() in lowered for t in concept.surface_terms):
            probe_terms.extend(concept.surface_terms)

    probes = [claim_text]
    if probe_terms:
        # dict.fromkeys dedupes while preserving order — a plain set would
        # make probe wording (and therefore retrieval) non-deterministic.
        probes.append(" ".join(dict.fromkeys(probe_terms)))

    retrieval = searcher.retrieve_union(probes, top_k=top_k)
    expansion_used = False

    if retrieval.found_nothing:
        from phases.query_expansion import expand_query

        expansion = expand_query(claim_text, llm_client, domain=getattr(ontology, "domain", ""))
        if expansion.terms:
            widened = searcher.retrieve_union(probes + list(expansion.terms), top_k=top_k)
            if not widened.found_nothing:
                retrieval = widened
                expansion_used = True

    if retrieval.found_nothing:
        return RetrievalResult(chunk_indices=[], expansion_used=expansion_used)
    return RetrievalResult(chunk_indices=list(retrieval.indices), expansion_used=expansion_used)
