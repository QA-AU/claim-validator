"""Phase 1: RAG Indexer - Create searchable index from document chunks.

Retrieval is local (TF-IDF over chunks) so indexing needs no embedding API key
and no network call. This keeps Phase 1 runnable with an Anthropic-only setup.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Set, Tuple, Dict, Any
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from phases.phase1_models import DocumentContent, RAGIndex

logger = logging.getLogger(__name__)

# Configuration
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 100
MAX_FEATURES = 50000


def create_rag_index(documents: List[DocumentContent]) -> RAGIndex:
    """Create RAG index from documents."""
    logger.info(f"Creating RAG index for {len(documents)} documents")

    all_chunks = []
    all_metadata = []

    # Chunk all documents
    for doc in documents:
        chunks = chunk_text(doc.raw_text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
        all_chunks.extend(chunks)

        for i, chunk in enumerate(chunks):
            all_metadata.append(
                {
                    "source": doc.file_name,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                }
            )

    logger.info(f"Created {len(all_chunks)} chunks from {len(documents)} documents")

    if not all_chunks:
        raise ValueError("No text could be extracted from the provided documents")

    index = RAGIndex(
        chunks=all_chunks,
        embeddings=[],  # vectors live on the fitted matrix below, not as dense lists
        metadata=all_metadata,
    )

    _fit(index)
    logger.info(f"Indexed {index.matrix.shape[0]} chunks over {index.matrix.shape[1]} terms")

    return index


def _fit(index: RAGIndex) -> None:
    """Fit the retrieval model over an index's chunks, in place.

    Shared by the initial build and the rebuild-from-disk path. A single
    definition on purpose: if the two fitted different tokenisers, the same query
    would retrieve different chunks before and after a reload, and every stored
    citation would resolve against a different model than produced it.
    """
    vectorizer = TfidfVectorizer(
        max_features=MAX_FEATURES,
        stop_words="english",
        # API docs are full of tokens like "/repos/{owner}" and "client_id"
        token_pattern=r"(?u)\b\w[\w./_-]+\b",
    )
    index.matrix = vectorizer.fit_transform(index.chunks)
    index.vectorizer = vectorizer


def rebuild_index(chunks: List[str], metadata: List[Dict[str, Any]]) -> RAGIndex:
    """Rebuild a searchable index from a persisted chunk stream.

    Chunk indices are positional, so the order of `chunks` must be exactly what
    was stored — a stored citation of "chunk 412" means the 412th entry, and
    reordering would silently repoint every citation in the ontology at the wrong
    passage.

    The TF-IDF model is refitted rather than restored. Fitting is local and fast,
    and identical input gives an identical model, so nothing is lost.
    """
    if not chunks:
        raise ValueError("Cannot rebuild a retrieval index from zero chunks")

    index = RAGIndex(chunks=list(chunks), embeddings=[], metadata=list(metadata))
    _fit(index)

    logger.info(f"Rebuilt retrieval index from {len(index.chunks)} persisted chunks")
    return index


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping chunks."""
    chunks = []
    start = 0

    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end].strip()

        if chunk:  # Only add non-empty chunks
            chunks.append(chunk)

        if end >= len(text):
            break

        start = end - overlap

    return chunks


def retrieve_relevant_chunks(query: str, rag_index: RAGIndex, top_k: int = 5) -> List[Tuple[int, str, float]]:
    """Retrieve relevant chunks for a query.

    Returns (chunk_index, chunk_text, score) so callers can look up metadata.
    """
    logger.info(f"Retrieving top {top_k} chunks for query: {query[:50]}...")

    vectorizer = getattr(rag_index, "vectorizer", None)
    matrix = getattr(rag_index, "matrix", None)
    if vectorizer is None or matrix is None:
        raise ValueError("RAG index was not built — call create_rag_index() first")

    query_vector = vectorizer.transform([query])
    similarities = cosine_similarity(query_vector, matrix)[0]

    # Get top k
    top_indices = np.argsort(similarities)[-top_k:][::-1]

    results = []
    for idx in top_indices:
        idx = int(idx)
        score = float(similarities[idx])
        results.append((idx, rag_index.chunks[idx], score))

        logger.debug(f"  {score:.3f}: {rag_index.chunks[idx][:50]}...")

    return results


# Below this share of a probe's own terms appearing in the best-matching chunk,
# the match is being carried by one or two incidental words rather than by the
# subject of the query.
#
# Calibrated by measurement on the contract fixture (2026-08-15), where score
# alone could not separate the cases and term overlap could:
#
#   "payment terms net thirty invoicing schedule"  score 0.123  overlap 1/6  WRONG
#   "settle invoice thirty days interest"          score 0.191  overlap 5/5  right
#   "material breach insolvent administrator"      score 0.220  overlap 3/5  right
#
# The wrong match ranked the *termination* clause first because "thirty" appears
# in both it and the payment terms. A score threshold would have to sit between
# 0.123 and 0.191 to catch it — far too tight to trust. Overlap separates them
# with room to spare.
LOW_TERM_OVERLAP = 0.34

_TERM = re.compile(r"[a-z0-9]{3,}")


def probe_terms(query: str) -> List[str]:
    """Distinct meaningful words in a probe, lowercased."""
    return list(dict.fromkeys(_TERM.findall((query or "").lower())))


@dataclass
class Retrieval:
    """One retrieval: the text, where it came from, and how well it matched."""

    context: str
    indices: List[int]
    scores: List[float]
    # How much of the probe actually appears in the best-matching chunk. TF-IDF
    # scores a chunk that shares one common word, which is indistinguishable
    # from a real match by score alone.
    terms_in_probe: int = 0
    terms_matched: int = 0
    # Chunks contributed by semantic search, when a semantic index is attached.
    # Kept separate from `scores` so `found_nothing` keeps meaning "no word
    # matched" rather than being quietly satisfied by a semantic hit.
    semantic_hits: List[int] = field(default_factory=list)
    semantic_best: float = 0.0

    @property
    def max_score(self) -> float:
        return max(self.scores) if self.scores else 0.0

    @property
    def found_nothing(self) -> bool:
        """True when no returned chunk shared a single term with the probe.

        The chunks are still returned and still go into the prompt — argsort has
        to return something — but they were selected arbitrarily, not because
        they are relevant. Treating that as a normal retrieval is how a concept
        gets populated from unrelated text without anything saying so.
        """
        return self.max_score <= 0.0

    @property
    def term_overlap(self) -> float:
        """Share of the probe's terms present in the best chunk."""
        if not self.terms_in_probe:
            return 0.0
        return self.terms_matched / self.terms_in_probe

    @property
    def rescued_semantically(self) -> bool:
        """No word matched, but meaning did — a passage lexical search could never reach."""
        return self.found_nothing and bool(self.semantic_hits)

    @property
    def weakly_matched(self) -> bool:
        """Matched *something*, but on too little of the probe to trust.

        The case `found_nothing` cannot catch: a non-zero score carried by an
        incidental shared word, returning a passage about something else. This
        is the wrong-but-overlapping match that no score threshold separates
        cleanly.
        """
        return not self.found_nothing and self.term_overlap < LOW_TERM_OVERLAP


class RAGIndexSearcher:
    """Convenient interface for RAG index searching.

    Tracks the union of chunk indices it has returned. Retrieval is the only
    place that knows which parts of the document the extraction ever saw, and the
    finished ontology cannot recover it — so the searcher accumulates it as it
    goes. That union is what the coverage metric reports and what per-instance
    provenance is built on.
    """

    def __init__(self, rag_index: RAGIndex, semantic_index=None):
        self.rag_index = rag_index
        self.chunks_consulted: Set[int] = set()
        # Optional. Absent means retrieval behaves exactly as it always has.
        self.semantic = semantic_index

    @property
    def chunks_total(self) -> int:
        return len(self.rag_index.chunks)

    def search(self, query: str, top_k: int = 5) -> List[Tuple[int, str, float]]:
        """Search the index, recording which chunks were consulted."""
        results = retrieve_relevant_chunks(query, self.rag_index, top_k=top_k)
        # Every chunk returned here goes into a prompt, so it counts as consulted
        # even if it scored poorly — the model saw it either way.
        self.chunks_consulted.update(idx for idx, _, _ in results)
        return results

    def get_context(self, query: str, top_k: int = 5) -> str:
        """Get context string from top k chunks."""
        return self.retrieve(query, top_k=top_k).context

    def get_context_with_indices(self, query: str, top_k: int = 5) -> Tuple[str, List[int]]:
        """Get the context string plus the chunk indices behind it.

        Callers that need provenance for what they extract take the indices;
        `get_context` is the same call for callers that do not.
        """
        result = self.retrieve(query, top_k=top_k)
        return result.context, result.indices

    def retrieve(self, query: str, top_k: int = 5) -> "Retrieval":
        """Retrieve context, provenance, and how well the probe actually matched.

        The score matters because TF-IDF matches words, not meaning: two chunks
        sharing no terms score exactly 0.0, which is mathematically identical to
        no relationship at all. When every returned chunk scores zero, the
        "context" handed to the model is arbitrary text — and nothing else in
        the output distinguishes that from a good retrieval.
        """
        results = self.search(query, top_k=top_k)

        parts = []
        indices = []
        scores = []
        for idx, chunk, score in results:
            # The chunk number is in the marker so the model can cite which
            # passage an instance came from. Without a citable label, per-instance
            # provenance would have to be guessed after the fact.
            parts.append(f"[chunk {idx} | source: {self.source_of(idx)}]\n{chunk}")
            indices.append(idx)
            scores.append(score)

        return self._with_overlap(
            Retrieval(context="\n\n".join(parts), indices=indices, scores=scores), query
        )

    def retrieve_union(self, queries: List[str], top_k: int = 5) -> "Retrieval":
        """Retrieve for several probes and keep the best-scoring chunks overall.

        Exists because adding terms to a probe can *displace* the chunks the
        original probe found rather than supplement them. Measured on the GitHub
        spec: "What does the stargazers endpoint return?" retrieves the right
        passages, but the same question widened with a concept's generic surface
        terms ("REST API", "HTTP method", "Interact with") retrieves the spec
        header instead — the one distinctive word is outweighed by several
        common ones.

        Running the probes separately and merging on score means a widening term
        can only ever add a candidate, never push a better one out.
        """
        best: Dict[int, float] = {}
        for query in queries:
            if not query or not query.strip():
                continue
            for idx, _, score in self.search(query, top_k=top_k):
                if score > best.get(idx, -1.0):
                    best[idx] = score

        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:top_k]

        parts, indices, scores = [], [], []
        for idx, score in ranked:
            parts.append(f"[chunk {idx} | source: {self.source_of(idx)}]\n{self.rag_index.chunks[idx]}")
            indices.append(idx)
            scores.append(score)

        return self._with_overlap(
            Retrieval(context="\n\n".join(parts), indices=indices, scores=scores), query
        )

    def _with_overlap(self, retrieval: "Retrieval", query: str) -> "Retrieval":
        """Record how much of the probe appears in the best-matching chunk."""
        terms = probe_terms(query)
        retrieval.terms_in_probe = len(terms)
        if terms and retrieval.indices:
            best = self.rag_index.chunks[retrieval.indices[0]].lower()
            retrieval.terms_matched = sum(1 for t in terms if t in best)
        return retrieval

    def attach_semantic(self, semantic_index) -> None:
        """Add a semantic backend. Retrieval becomes hybrid; without one it is
        exactly as it was."""
        if semantic_index is not None and semantic_index.size != self.chunks_total:
            raise ValueError(
                f"Semantic index has {semantic_index.size} vectors but this index has "
                f"{self.chunks_total} chunks — row order is what ties a vector to its "
                f"passage, so a mismatched index would cite the wrong text"
            )
        self.semantic = semantic_index

    @property
    def has_semantic(self) -> bool:
        return getattr(self, "semantic", None) is not None

    def retrieve_hybrid(self, query: str, top_k: int = 5) -> "Retrieval":
        """Lexical and semantic results merged, each covering the other's blind spot.

        They fail differently. TF-IDF is exact on identifiers a model embeds
        poorly (`/repos/{owner}/{repo}`, `x-rate-limit-remaining` are strings,
        not concepts); embeddings catch paraphrase, which TF-IDF scores as
        exactly zero. Merging keeps both — a semantic hit can add a chunk
        lexical search missed, and cannot push out one it found.

        Scores from the two are not on a comparable scale, so they are not
        averaged. Each backend contributes its own best candidates.
        """
        lexical = self.retrieve(query, top_k=top_k)
        if not self.has_semantic:
            return lexical

        semantic_hits = self.semantic.search(query, top_k=top_k)
        if not semantic_hits:
            return lexical

        # Interleave, lexical first: an exact word match is the stronger signal
        # when it exists, and this keeps it at the top of the prompt.
        ordered: List[int] = []
        for lex_idx, sem in zip(lexical.indices, [i for i, _ in semantic_hits]):
            for candidate in (lex_idx, sem):
                if candidate not in ordered:
                    ordered.append(candidate)
        for extra in lexical.indices + [i for i, _ in semantic_hits]:
            if extra not in ordered:
                ordered.append(extra)
        ordered = ordered[:top_k]

        semantic_scores = dict(semantic_hits)
        lexical_scores = dict(zip(lexical.indices, lexical.scores))

        parts, scores = [], []
        for idx in ordered:
            parts.append(f"[chunk {idx} | source: {self.source_of(idx)}]\n{self.rag_index.chunks[idx]}")
            # Report the lexical score where there is one, so `found_nothing`
            # keeps meaning "no word matched" rather than being masked by a
            # semantic hit.
            scores.append(lexical_scores.get(idx, 0.0))

        result = Retrieval(context="\n\n".join(parts), indices=ordered, scores=scores)
        result = self._with_overlap(result, query)
        result.semantic_hits = [i for i, _ in semantic_hits]
        result.semantic_best = max((s for _, s in semantic_hits), default=0.0)
        return result

    def source_of(self, chunk_index: int) -> str:
        """Which document a chunk came from."""
        if 0 <= chunk_index < len(self.rag_index.metadata):
            return self.rag_index.metadata[chunk_index].get("source", "unknown")
        return "unknown"
