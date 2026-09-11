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

import re
from dataclasses import dataclass, field
from typing import List

ASK_TOP_K = 5

# A coordinating "and" joining two independent clauses, not "X and Y" inside
# a short noun phrase — requires a preceding comma or semicolon, which is
# how a claim's own sentence structure usually signals "this is actually
# two facts", not one list (see issue #3). Deliberately conservative in one
# direction only: missing a genuinely compound claim just means today's
# single-probe retrieval, no regression — the risk here runs toward
# under-splitting, not over-splitting, and over-splitting only costs one
# extra free local search, never a wrong verdict on its own.
_CLAUSE_CONJUNCTION_RE = re.compile(r"[,;]\s+and\s+(?=[a-z])")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
# A backtick or double-quoted span — a coordinating conjunction found inside
# one of these is part of the quoted/code text, not a clause boundary
# (`"A, and B"` as a literal string; `` `X, and Y` `` as a code span).
_PROTECTED_SPAN_RE = re.compile(r"`[^`]*`|\"[^\"]*\"")


def _split_sentence_into_clauses(sentence: str) -> List[str]:
    protected = [m.span() for m in _PROTECTED_SPAN_RE.finditer(sentence)]

    def _inside_protected(pos: int) -> bool:
        return any(start <= pos < end for start, end in protected)

    pieces, cursor = [], 0
    for m in _CLAUSE_CONJUNCTION_RE.finditer(sentence):
        if _inside_protected(m.start()):
            continue
        pieces.append(sentence[cursor:m.start()].strip())
        cursor = m.end()
    # The sentence-split step (_split_claim_into_clauses) keeps a sentence's
    # own terminal punctuation attached to it, so the LAST clause here
    # otherwise carries a trailing "." into what becomes a retrieval query
    # — harmless to TF-IDF but pointless. Only the trailing character is
    # trimmed, never anything mid-clause (a real "20 seconds." is fine;
    # this just drops the very last full stop).
    pieces.append(sentence[cursor:].strip().rstrip(".!?"))
    return [p for p in pieces if p]


def _split_claim_into_clauses(claim_text: str) -> List[str]:
    """Candidate sub-clauses of a claim, for retrieval only — the judge
    still ever sees the whole original claim text, never a piece of it (see
    issue #3: this widens what gets cited, it does not split the claim
    itself or its verdict). Empty (not `[claim_text]`) when nothing splits,
    so a caller can tell "not compound" from "one clause" without a length
    check on every call.

    A near relative of numeric_threshold_check.py's `_sentences()` — same
    sentence-boundary idea, but a different second-level split (coordinating
    conjunctions here vs. markdown headings there) for a different kind of
    text (a short generated claim vs. a retrieved document passage). Kept
    separate rather than forced into one shared utility for two purposes
    that only look similar at the sentence-splitting layer.
    """
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(claim_text) if s.strip()]
    clauses: List[str] = []
    for sentence in sentences:
        clauses.extend(_split_sentence_into_clauses(sentence))
    return clauses if len(clauses) > 1 else []


@dataclass
class RetrievalResult:
    chunk_indices: List[int] = field(default_factory=list)
    expansion_used: bool = False
    # See _split_claim_into_clauses — True only when the claim looked
    # compound AND a per-clause retrieval actually added a chunk the
    # whole-claim probe alone didn't already have.
    widened_for_clauses: bool = False


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

    indices = set(retrieval.indices) if not retrieval.found_nothing else set()

    # Issue #3: a claim stating multiple facts can lose the less prominent
    # one to the more prominent one's vocabulary in a single combined query
    # — see retrieve_union's own docstring for the measured mechanism.
    # Widening the SAME probe set doesn't fix this: retrieve_union's merge
    # step trims back to one shared top_k regardless of how many probes go
    # in, so the losing fact's passage can still be crowded out even inside
    # a "union". A genuinely separate, independently-capped retrieval per
    # clause is the only way to guarantee each fact gets its own top_k.
    widened_for_clauses = False
    for clause in _split_claim_into_clauses(claim_text):
        extra = searcher.retrieve(clause, top_k=top_k)
        if extra.found_nothing:
            continue  # this clause's own retrieval found nothing real either
        new_indices = set(extra.indices) - indices
        if new_indices:
            indices |= new_indices
            widened_for_clauses = True

    if not indices:
        return RetrievalResult(chunk_indices=[], expansion_used=expansion_used)
    return RetrievalResult(chunk_indices=sorted(indices), expansion_used=expansion_used,
                            widened_for_clauses=widened_for_clauses)
