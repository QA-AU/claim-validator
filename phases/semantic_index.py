"""Optional semantic retrieval, to catch what word matching cannot.

TF-IDF matches words. A document saying "callers are throttled once they exceed
their allowance" scores **exactly 0.0** against "rate limit", and no amount of
reweighting fixes that — the words simply are not there.

Two cheaper routes were tried first and are recorded so they are not retried:

* **LSA** over the existing TF-IDF matrix. An unseen query term is all-zeros
  before reduction and all-zeros after, so it cannot supply vocabulary the
  document lacks. Measured 0.0000 both ways.
* **Query expansion** — asking the model for the document's likely wording.
  Works (0.000 → 0.399), costs a call, and fails when the guesses miss.

Embeddings are the actual fix, because the model was trained on text elsewhere
and already knows the words are related. Measured on the same document:

    query "rate limit"                    TF-IDF 0.0000   embeddings 0.6547
    query "how many requests am I allowed" TF-IDF 0.0000   embeddings 0.6672

The second query shares **no words at all** with the passage it correctly finds.

### Why it is optional, and off by default

Embedding runs at roughly 24 chunks/second on CPU. That is instant for a
contract and about **ten minutes** for the 14,356-chunk GitHub spec. A cost like
that must be asked for, not incurred silently by every extraction — so this is
built on request, cached to disk, and absent unless someone chose it.

The pipeline runs identically without `fastembed` installed. `available()` says
which, and retrieval falls back to lexical rather than failing.

### Why hybrid rather than replacement

Lexical and semantic fail differently. TF-IDF is exact on identifiers a model
embeds poorly — `/repos/{owner}/{repo}` and `x-rate-limit-remaining` are strings,
not concepts. Embeddings handle paraphrase, which TF-IDF cannot see at all.
Merging on best score means each covers the other's blind spot, and neither can
displace the other's hits.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_FILE = "embeddings.npy"

# Throughput measured on CPU, 2026-08-15. Used only to warn about cost before
# a build starts — a ten-minute wait should never be a surprise.
CHUNKS_PER_SECOND = 24


def available() -> bool:
    """Whether semantic retrieval can run at all."""
    try:
        import fastembed  # noqa: F401
        import numpy  # noqa: F401

        return True
    except ImportError:
        return False


def estimate_seconds(chunk_count: int) -> float:
    return chunk_count / CHUNKS_PER_SECOND if chunk_count else 0.0


@dataclass
class SemanticIndex:
    """Chunk embeddings, with the same positional contract as the chunk list.

    Row *i* is the vector for chunk *i*. That alignment is what lets a semantic
    hit be merged with a lexical one and cited as the same chunk — so a stored
    index is only valid for the chunk stream that produced it, and the row count
    is checked on load rather than trusted.
    """

    vectors: "object" = None  # numpy array, kept untyped so numpy stays optional
    model_name: str = DEFAULT_MODEL

    @property
    def size(self) -> int:
        return 0 if self.vectors is None else int(self.vectors.shape[0])

    def search(self, query: str, top_k: int = 8) -> List[Tuple[int, float]]:
        """Chunk indices most similar in meaning, best first."""
        if self.vectors is None or not query.strip():
            return []

        import numpy as np

        vector = _embed([query], self.model_name)[0]
        norm = np.linalg.norm(vector)
        if norm == 0:
            return []

        scores = self.vectors @ (vector / norm)
        top = np.argsort(scores)[-top_k:][::-1]
        return [(int(i), float(scores[i])) for i in top]

    def save(self, path: Path) -> None:
        import numpy as np

        np.save(str(path), self.vectors)
        logger.info(f"Saved {self.size} embeddings to {path}")

    @classmethod
    def load(cls, path: Path, expected_chunks: int, model_name: str = DEFAULT_MODEL):
        """Load embeddings, refusing any that no longer match the chunk stream.

        A stale file is worse than none: row *i* would point at a different
        passage than chunk *i*, so every semantic citation would be wrong while
        looking perfectly normal.
        """
        if not path.exists():
            return None

        try:
            import numpy as np

            vectors = np.load(str(path))
        except Exception as e:
            logger.warning(f"Could not read embeddings at {path}: {e}")
            return None

        if vectors.shape[0] != expected_chunks:
            logger.warning(
                f"Embeddings at {path} have {vectors.shape[0]} rows but the index has "
                f"{expected_chunks} chunks — discarding, since row order is the only "
                f"thing tying a vector to its passage"
            )
            return None

        return cls(vectors=vectors, model_name=model_name)


def _embed(texts: List[str], model_name: str):
    """Normalised embeddings for a list of texts."""
    import numpy as np
    from fastembed import TextEmbedding

    model = _model(model_name)
    vectors = np.array(list(model.embed(texts)), dtype="float32")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


_MODELS = {}


def _model(model_name: str):
    """One model instance per name — loading it takes seconds."""
    if model_name not in _MODELS:
        from fastembed import TextEmbedding

        logger.info(f"Loading embedding model {model_name}")
        _MODELS[model_name] = TextEmbedding(model_name=model_name)
    return _MODELS[model_name]


def build(chunks: List[str], model_name: str = DEFAULT_MODEL) -> Optional[SemanticIndex]:
    """Embed every chunk. Slow enough that callers should warn first."""
    if not available():
        logger.warning("fastembed is not installed; semantic retrieval unavailable")
        return None
    if not chunks:
        return None

    logger.info(
        f"Embedding {len(chunks)} chunks (~{estimate_seconds(len(chunks)):.0f}s)"
    )
    return SemanticIndex(vectors=_embed(chunks, model_name), model_name=model_name)
