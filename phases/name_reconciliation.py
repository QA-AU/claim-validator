"""Decide when two passes are naming the same thing.

Gap detection compares slugs, and the census and the extractor routinely use
different vocabulary for one instance:

    extraction   list_orders                    Authorization Bearer Token
    census       GET /orders                    Authorization: Bearer <token> header

Measured live, `api_operation` reported **48 of 48 missed while 18 had already
been extracted**. Its "38% capture" measured vocabulary, not coverage — and a
plan built on it would send targeted extraction to re-extract instances the
ontology already had, adding a duplicate of each under a second name.

### Why this does not cost the census its independence

The obvious fix — showing the census what extraction already found — would ruin
it. A census primed with the answers is no longer separate evidence about what
the document contains, and its whole value is being independent.

Reconciliation avoids that by happening **afterwards**. The census is taken
blind; only then are the two finished lists compared. Nothing about the
comparison can reach back and influence what the census saw.

### Three layers, cheapest first

1. **Exact** — identical slugs. Free, and handles most of every real run.
2. **Lexical** — token containment above a high threshold, and only when a
   single candidate reaches it. Free, and catches the paraphrase cases
   (`Authorization Bearer Token` ↔ `Authorization: Bearer <token> header`).
3. **Judged** — one batched model call per concept for whatever remains.
   `list_orders` ↔ `GET /orders` needs knowledge of the domain, not string
   similarity, and no threshold separates it from `create_orders`.

Layer 3 is skipped entirely when no client is supplied, so reconciliation always
works — it just does less.

### Both mistakes are real, so ambiguity is reported rather than resolved

A false **match** hides a genuine gap: the instance is missing and nothing says
so. A false **miss** sends extraction to re-fetch something already present,
producing a duplicate under a different name.

Neither is acceptable silently, so a candidate that is not clearly one or the
other is recorded as `ambiguous` and left to a person. Matching is also strictly
one-to-one: once a sampled name is claimed, a second census name claiming it is
ambiguous rather than a match, because "GET /orders and DELETE /orders are both
list_orders" is a sign the alignment is wrong, not a pair of matches.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MATCH_EXACT = "exact"
MATCH_LEXICAL = "lexical"
MATCH_JUDGED = "judged"

# Token containment needed for a free lexical match. High on purpose: this layer
# runs without a model and cannot explain itself, so it should only fire when
# the two strings are near-restatements of each other.
LEXICAL_THRESHOLD = 0.8

# Census names per judging call.
RECONCILE_BATCH = 25

# Tokens that carry no identity. Kept deliberately short — an aggressive stop
# list makes unrelated names look alike, which is the failure this module exists
# to avoid.
_NOISE = {"the", "a", "an", "of", "for", "to", "and", "or"}


def tokens(name: str) -> set:
    """Identity-bearing words in a name, lowercased."""
    parts = re.split(r"[^0-9a-zA-Z]+", (name or "").lower())
    return {p for p in parts if p and p not in _NOISE}


def containment(a: str, b: str) -> float:
    """Shared tokens as a share of the shorter name.

    Containment rather than Jaccard because one pass is often more verbose:
    "Authorization: Bearer <token> header" says everything "Authorization Bearer
    Token" does and more, and that should score 1.0 rather than be penalised for
    the extra words.
    """
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


@dataclass
class Reconciliation:
    """Which census names correspond to instances already extracted."""

    concept: str = ""
    # census name -> the sampled name it turned out to be
    matched: Dict[str, str] = field(default_factory=dict)
    # census name -> how the match was decided
    method: Dict[str, str] = field(default_factory=dict)
    # Census names with no counterpart: the real gap.
    unmatched: List[str] = field(default_factory=list)
    # (census name, candidates) that could not be settled either way.
    ambiguous: List[Tuple[str, List[str]]] = field(default_factory=list)
    calls_made: int = 0

    @property
    def by_method(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for method in self.method.values():
            counts[method] = counts.get(method, 0) + 1
        return counts

    def review_flags(self) -> List[str]:
        flags = []
        if self.ambiguous:
            flags.append(
                f"{len(self.ambiguous)} census name(s) could not be matched or ruled out; "
                f"they are counted as missing, which may re-extract an instance the "
                f"ontology already has under another name"
            )
        renamed = sum(1 for m in self.method.values() if m != MATCH_EXACT)
        if renamed:
            flags.append(
                f"{renamed} instance(s) matched only after reconciliation — the two passes "
                f"name them differently"
            )
        return flags

    def to_dict(self) -> Dict[str, Any]:
        return {
            "concept": self.concept,
            "matched": self.matched,
            "method": self.method,
            "by_method": self.by_method,
            "unmatched": self.unmatched,
            "ambiguous": [{"census": c, "candidates": k} for c, k in self.ambiguous],
            "calls_made": self.calls_made,
            "review_flags": self.review_flags(),
        }


def _parse_pairs(response: str) -> List[Dict[str, Any]]:
    if not response:
        return []
    fenced = re.search(r"```(?:json)?\s*(.+?)```", response, re.DOTALL)
    if fenced:
        response = fenced.group(1)
    match = re.search(r"\[.*\]", response, re.DOTALL)
    if not match:
        logger.warning(f"[Reconcile] Unparseable response: {response[:120]!r}")
        return []
    try:
        data = json.loads(match.group())
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    except json.JSONDecodeError:
        logger.warning(f"[Reconcile] Malformed JSON: {response[:120]!r}")
        return []


def _judge(
    concept: str,
    description: str,
    census_names: List[str],
    sampled_names: List[str],
    llm_client,
    batch_size: int,
) -> Tuple[Dict[str, str], List[Tuple[str, List[str]]], int]:
    """Ask which remaining census names are things already extracted."""
    decided: Dict[str, str] = {}
    unsure: List[Tuple[str, List[str]]] = []
    calls = 0

    for start in range(0, len(census_names), batch_size):
        batch = census_names[start : start + batch_size]

        prompt = f"""Two passes over the same document listed instances of one concept, using
different naming conventions. Say which names refer to the same thing.

CONCEPT: {concept}
MEANING: {description or concept}

LIST A — names from the first pass:
{chr(10).join(f"- {n}" for n in sampled_names)}

LIST B — names from the second pass:
{chr(10).join(f"- {n}" for n in batch)}

Return a JSON array, one object per name in LIST B:
- b: the LIST B name, exactly as given
- a: the LIST A name it refers to, exactly as given — or null if LIST A does not
     contain this thing at all
- confident: true only if you are sure. Use false when it could plausibly be
     more than one LIST A entry, or you cannot tell

Match on what the thing *is*, not on spelling: "list_orders" and "GET /orders"
are the same operation. Two different things that merely look alike are not a
match — "GET /orders" and "DELETE /orders" are different operations even though
one word separates them.

Return ONLY the JSON array."""

        try:
            items = _parse_pairs(llm_client.generate(prompt))
        except Exception as e:
            logger.error(f"[Reconcile] Batch {start // batch_size + 1} failed: {e}")
            calls += 1
            continue
        calls += 1

        allowed = set(sampled_names)
        for item in items:
            b = str(item.get("b", "")).strip()
            if b not in batch:
                continue
            a = item.get("a")
            a = str(a).strip() if a is not None else ""

            if not a:
                continue  # genuinely absent from list A — leave it unmatched
            if a not in allowed:
                # A name the model invented cannot stand as evidence that the
                # ontology already holds this instance.
                logger.debug(f"[Reconcile] {b!r} matched to unknown name {a!r}; ignoring")
                continue
            if not bool(item.get("confident", False)):
                unsure.append((b, [a]))
                continue

            decided[b] = a

    return decided, unsure, calls


def reconcile_names(
    sampled_names: List[str],
    census_names: List[str],
    concept: str = "",
    description: str = "",
    llm_client=None,
    batch_size: int = RECONCILE_BATCH,
    threshold: float = LEXICAL_THRESHOLD,
) -> Reconciliation:
    """Align two independently-produced name lists for the same concept."""
    from phases.phase1_models import slugify

    result = Reconciliation(concept=concept)
    if not census_names:
        return result

    by_slug = {slugify(n): n for n in sampled_names}
    # One-to-one: a sampled name, once claimed, is out of the running.
    claimed = set()
    remaining = []

    # --- layer 1: exact ---
    for name in census_names:
        sampled = by_slug.get(slugify(name))
        if sampled is not None and sampled not in claimed:
            result.matched[name] = sampled
            result.method[name] = MATCH_EXACT
            claimed.add(sampled)
        else:
            remaining.append(name)

    # --- layer 2: lexical ---
    still: List[str] = []
    # Names where several candidates tied. String similarity cannot choose, but
    # the judge sometimes can — "/orders" against three operations is genuinely
    # undecidable, while other ties are not — so a tie is passed down rather
    # than settled here. The candidates are kept to report if it stays unsolved.
    contested: Dict[str, List[str]] = {}

    for name in remaining:
        scored = [
            (containment(name, s), s) for s in sampled_names if s not in claimed
        ]
        top = [s for score, s in scored if score >= threshold]

        if len(top) == 1:
            result.matched[name] = top[0]
            result.method[name] = MATCH_LEXICAL
            claimed.add(top[0])
        elif len(top) > 1:
            contested[name] = sorted(top)
            still.append(name)
        else:
            still.append(name)

    # --- layer 3: judged ---
    if still and llm_client is not None:
        available = [s for s in sampled_names if s not in claimed]
        if available:
            decided, unsure, calls = _judge(
                concept, description, still, available, llm_client, batch_size
            )
            result.calls_made = calls

            unsure_names = {name for name, _ in unsure}

            for name in still:
                sampled = decided.get(name)
                if sampled is not None and sampled not in claimed:
                    result.matched[name] = sampled
                    result.method[name] = MATCH_JUDGED
                    claimed.add(sampled)
                elif sampled is not None:
                    # Two census names claimed one sampled name — the alignment
                    # is wrong somewhere, so neither is asserted.
                    result.ambiguous.append((name, [sampled]))
                elif name in unsure_names:
                    # The judge looked and could not tell. Report the candidates
                    # it was weighing, or the lexical tie if there were none.
                    candidates = next(c for n, c in unsure if n == name)
                    result.ambiguous.append((name, contested.get(name) or candidates))
                elif name in contested:
                    # Tied lexically and the judge did not resolve it either.
                    result.ambiguous.append((name, contested[name]))
                else:
                    result.unmatched.append(name)
        else:
            for name in still:
                if name in contested:
                    result.ambiguous.append((name, contested[name]))
                else:
                    result.unmatched.append(name)
    else:
        for name in still:
            if name in contested:
                result.ambiguous.append((name, contested[name]))
            else:
                result.unmatched.append(name)

    # Anything flagged ambiguous is *not* treated as present. It stays out of
    # `matched`, so the gap is preserved and reported rather than assumed away.
    ambiguous_names = {name for name, _ in result.ambiguous}
    result.unmatched = [
        n for n in census_names
        if n not in result.matched and n not in ambiguous_names
    ]

    logger.info(
        f"[Reconcile] {concept or 'names'}: {len(result.matched)} matched "
        f"({result.by_method}), {len(result.unmatched)} unmatched, "
        f"{len(result.ambiguous)} ambiguous"
        + (f", {result.calls_made} call(s)" if result.calls_made else "")
    )
    return result


# ---------------------------------------------------------------------------
# Matching across concepts, not only within one
# ---------------------------------------------------------------------------
#
# `reconcile_names` compares one concept's extracted names against the same
# concept's census names. That is right when both passes agree about what kind
# of thing each instance is, and wrong when they do not.
#
# Measured: the one-pass census filed `GET /orders` under `endpoint` while
# extraction had filed it under `api_operation`. Comparing within the concept
# reported 22 of `api_operation`'s instances as missing when the census had
# found every one of them — under a different label. All 22 were present; none
# was a gap. A capture ratio built on that is not a measure of completeness, it
# is a measure of whether two passes chose the same word.
#
# So capture is asked as "did extraction find this thing at all", and the
# concept disagreement is reported separately, because it is a real finding —
# usually that the ontology holds two concept types describing the same things.


@dataclass
class CrossReconciliation:
    """One concept's census names, matched against everything extraction found."""

    concept: str
    # census name -> (extracted name, the concept it was filed under)
    matched: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    unmatched: List[str] = field(default_factory=list)
    ambiguous: List[Tuple[str, List[str]]] = field(default_factory=list)
    calls_made: int = 0

    @property
    def elsewhere(self) -> Dict[str, Tuple[str, str]]:
        """Matches found under a *different* concept than the census used."""
        return {name: pair for name, pair in self.matched.items()
                if pair[1] != self.concept}

    def capture(self, census_total: int) -> Optional[float]:
        if not census_total:
            return None
        return min(len(self.matched) / census_total, 1.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "concept": self.concept,
            "matched": len(self.matched),
            "matched_elsewhere": len(self.elsewhere),
            "unmatched": self.unmatched,
            "ambiguous": len(self.ambiguous),
            "filed_elsewhere": {n: c for n, (_, c) in self.elsewhere.items()},
        }


def reconcile_across_concepts(
    extracted_by_concept: Dict[str, List[str]],
    census_by_concept: Dict[str, List[str]],
    llm_client=None,
    threshold: float = LEXICAL_THRESHOLD,
) -> Dict[str, CrossReconciliation]:
    """Match each concept's census names against every extracted name.

    Returns one `CrossReconciliation` per census concept. A match found under a
    different concept still counts as captured — the instance is in the
    ontology, which is what completeness asks — and is reported in `elsewhere`
    so the disagreement is visible rather than absorbed.
    """
    # Name -> the concept extraction filed it under. First filing wins, and a
    # name appearing under two concepts is itself the disagreement being
    # measured, so it does not need resolving here.
    home: Dict[str, str] = {}
    everything: List[str] = []
    for concept, names in extracted_by_concept.items():
        for name in names:
            everything.append(name)
            home.setdefault(name, concept)

    out: Dict[str, CrossReconciliation] = {}
    for concept, census_names in census_by_concept.items():
        result = CrossReconciliation(concept=concept)
        if not census_names:
            out[concept] = result
            continue

        inner = reconcile_names(
            everything, census_names, concept=concept,
            llm_client=llm_client, threshold=threshold,
        )
        result.calls_made = inner.calls_made
        result.unmatched = list(inner.unmatched)
        result.ambiguous = list(inner.ambiguous)
        for census_name, extracted_name in inner.matched.items():
            result.matched[census_name] = (extracted_name, home.get(extracted_name, ""))
        out[concept] = result

    return out
