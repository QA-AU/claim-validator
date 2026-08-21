"""Ask the model for the words a document might use, when ours do not match.

Retrieval is lexical, so a query fails completely when the document expresses
the same idea in different words. `todo/04` records the case: a document saying
"callers are throttled once they exceed their allowance" scores **exactly 0.0**
against "rate limit".

### Why not embeddings, and why not LSA

The obvious fix is semantic retrieval, which needs vectors trained on an
external corpus — a dependency and a model download.

LSA over the existing TF-IDF matrix looks like a free alternative and is not.
Measured 2026-08-15 on a document using only "throttled"/"quota":

    query "rate limit" -> TF-IDF vector has 0 non-zero entries
    TF-IDF best score  0.0000
    LSA    best score  0.0000

An unseen term is all-zeros *before* reduction, so it is all-zeros after. LSA
relates terms that co-occur **in this document**; it cannot supply vocabulary
the document lacks. That is the whole problem, so LSA cannot touch it.

### What this does instead

The pipeline already has a model that knows synonyms. Asking it for the wordings
a document might use costs one small call and needs nothing new:

    query alone                       score 0.000  found nothing
    query + expansions, merged        score 0.399  found the right passage

Expansions are used as **separate probes** merged by `retrieve_union`, never
appended to the query — appending common words displaces the distinctive one,
which is the failure recorded in `todo/04` under the stargazers case.

### What it is not

This is vocabulary expansion, not semantic matching. It fails when the model's
guesses miss the document's actual wording, and it cannot rank by meaning. It
narrows the gap that embeddings would close properly; it does not close it.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_EXPANSION_TERMS = 8


@dataclass
class Expansion:
    """Alternative wordings for one query."""

    query: str
    terms: List[str] = field(default_factory=list)
    from_cache: bool = False

    @property
    def probes(self) -> List[str]:
        """The query plus each expansion, as separate probes.

        Separate on purpose: merging them into one string lets several common
        words outweigh the one distinctive term, which is how a stargazers
        query came to retrieve a spec header.
        """
        return [self.query] + self.terms

    def to_dict(self) -> Dict:
        return {"query": self.query, "terms": self.terms, "from_cache": self.from_cache}


class ExpansionCache:
    """Remembers expansions so the same question is not paid for twice."""

    def __init__(self):
        self._entries: Dict[str, List[str]] = {}

    @staticmethod
    def _key(query: str) -> str:
        return " ".join((query or "").lower().split())

    def get(self, query: str) -> Optional[List[str]]:
        return self._entries.get(self._key(query))

    def put(self, query: str, terms: List[str]) -> None:
        self._entries[self._key(query)] = terms

    def __len__(self) -> int:
        return len(self._entries)


def _parse_terms(response: str, max_terms: int) -> List[str]:
    if not response:
        return []

    fenced = re.search(r"```(?:json)?\s*(.+?)```", response, re.DOTALL)
    if fenced:
        response = fenced.group(1)

    match = re.search(r"\[.*\]", response, re.DOTALL)
    if not match:
        logger.warning(f"[Expansion] Unparseable response: {response[:100]!r}")
        return []

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        logger.warning(f"[Expansion] Malformed JSON: {response[:100]!r}")
        return []

    terms = []
    for item in data if isinstance(data, list) else []:
        term = str(item).strip()
        # Long "terms" are sentences and make poor probes.
        if term and len(term) <= 60 and term.lower() not in {t.lower() for t in terms}:
            terms.append(term)
    return terms[:max_terms]


def expand_query(
    query: str,
    llm_client,
    cache: Optional[ExpansionCache] = None,
    max_terms: int = MAX_EXPANSION_TERMS,
    domain: str = "",
) -> Expansion:
    """Alternative wordings a document might use for this query.

    Returns an empty expansion rather than raising if anything goes wrong — the
    original query still works, so a failed expansion should cost nothing but
    the call.
    """
    if not query or not query.strip():
        return Expansion(query=query or "")

    if cache is not None:
        cached = cache.get(query)
        if cached is not None:
            return Expansion(query=query, terms=list(cached), from_cache=True)

    prompt = f"""A search is being run over a document using literal word matching, so it
finds nothing when the document says the same thing in different words.

SEARCH: {query}
{f"THE DOCUMENT IS: {domain}" if domain else ""}

List the words and short phrases a document might actually use for this idea
instead. Include:
- synonyms and near-synonyms
- verb forms ("throttled" for "rate limit", "settle" for "pay")
- abbreviations and technical spellings
- the formal or legal register a document might prefer

Each entry must be a word or short phrase, never a sentence. Do not repeat the
words already in the search. Return ONLY a JSON array of strings."""

    try:
        terms = _parse_terms(llm_client.generate(prompt), max_terms)
    except Exception as e:
        logger.error(f"[Expansion] Failed for {query!r}: {e}")
        terms = []

    if cache is not None:
        cache.put(query, terms)

    logger.info(f"[Expansion] {query!r} -> {len(terms)} alternative wording(s)")
    return Expansion(query=query, terms=terms)


def retrieve_expanded(query: str, searcher, llm_client, top_k: int = 8, **kwargs):
    """Retrieve for a query, falling back to expansion only when it finds nothing.

    Deliberately lazy. Most queries match, and expanding every one would add a
    model call per retrieval for no benefit. The call is spent exactly when the
    lexical search has already failed — which is the case expansion exists for.

    Returns `(retrieval, expansion_or_None)`.
    """
    direct = searcher.retrieve(query, top_k=top_k)
    if not direct.found_nothing:
        return direct, None

    logger.info(f"[Expansion] {query!r} matched nothing; asking for other wordings")
    expansion = expand_query(query, llm_client, **kwargs)
    if not expansion.terms:
        return direct, expansion

    return searcher.retrieve_union(expansion.probes, top_k=top_k), expansion
