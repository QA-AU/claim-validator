"""What the submitted claims never address, using the document's own census
as ground truth — kept as its own report, never blended into per-claim
verdicts. A claim can be perfectly entailed and still leave this report
looking exactly the same: correctness and completeness are different
questions, and conflating them is the specific mistake this module exists to
avoid (see `phases/completeness.py`'s docstring in the source repo for the
same argument made about this pipeline's own extraction).

Deliberately does not reuse `completeness.py::measure()` / `compare_to_sample`
— both read `ontology.concept_types[i].instances`, comparing the document
against *this pipeline's own extraction*. Here the comparison is against an
*externally supplied* claim list, which has no `Ontology` shape at all.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from phases.census import census_many, census_repeated
from phases.completeness import document_sections, sections_read
from phases.name_reconciliation import reconcile_names
from phases.phase1_models import slugify

from claimvalidator.claim_shims import ResolvedClaim

logger = logging.getLogger(__name__)

# `census_repeated`'s `probable` names and `census_many`'s `chunk_of` keys
# come from two independent LLM calls that each name the same real
# instances in their own words — "Escape key closes dialog" from one pass,
# "Pressing Escape closes the dialog" from the other. An exact slugify()
# match between them misses constantly on free-text paraphrases (found
# live: every keyboard-interaction instance in a real gap report read
# "no verified citation" despite the census plainly having found them,
# because the two passes' names never matched byte-for-byte) even though
# it works fine on short, literal identifiers (a proto field name is
# unlikely to be phrased two different ways). Token-overlap matching
# below closes that gap without touching phases/census.py, which this
# project keeps verbatim from the source repo.
_STOPWORDS = {
    "a", "an", "the", "to", "of", "is", "are", "was", "were", "with", "on",
    "in", "for", "and", "or", "this", "that", "at", "by", "as", "be",
}
_FUZZY_MATCH_THRESHOLD = 0.5
# Longer suffixes checked first so "closing" strips to "clos" in one pass
# rather than partially matching "ing" after already trying "s".
_STEM_SUFFIXES = ("ing", "edly", "ed", "es", "s")


def _stem(word: str) -> str:
    """Crude English-suffix stripping — not a real stemmer (Porter etc.),
    deliberately simple. Exists because a real diagnostic run (comparing
    two independent census passes' names for the same real instance)
    found "buttons" vs "button" scoring 0.000 overlap under exact word
    matching alone, even though a human reads them as the same word.
    Guarded against the obvious false-stem cases: short words ("role",
    "was") and words ending in a double "s" ("class", "address",
    "process" are not plurals of "clas"/"addres"/"proces")."""
    if len(word) <= 4 or word.endswith("ss"):
        return word
    for suffix in _STEM_SUFFIXES:
        if word.endswith(suffix):
            stem = word[: -len(suffix)]
            if len(stem) >= 3:
                return stem
    return word


def _content_tokens(text: str) -> Set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {_stem(w) for w in words if w not in _STOPWORDS}


def _best_fuzzy_match(display_name: str, candidates: List[str]) -> Optional[str]:
    """The candidate whose content-word overlap with `display_name` is
    highest, by Jaccard similarity — or None if nothing clears
    `_FUZZY_MATCH_THRESHOLD`. `candidates` is the *other* census pass's own
    independently-generated names for the same concept.

    Known boundary: word-overlap matching only closes a *lexical* gap
    (plurals, verb forms, word order) — it cannot recognize two
    phrasings of the same fact that share almost no vocabulary.
    Diagnosed on a real SemVer validation run: "ASCII alphanumerics and
    hyphens in build metadata" (census_repeated's name) and "Build
    metadata identifiers composition" (census_many's name for the
    literal same rule) scored 0.286 — the best of five real candidates,
    still well under threshold. Lowering the threshold to catch it would
    also start matching genuinely unrelated instances (a
    deliberately-unrelated pair in this module's own tests scores 0.111,
    not far below 0.286).

    This function is deliberately left exactly as it was (see issue #2)
    — it is still the free, fast, already-tested first attempt. The
    class of paraphrase it cannot see (real semantic similarity, not
    lexical overlap) is now handled one level up, in `build_gap_report`,
    which falls back to `phases.name_reconciliation.reconcile_names`'s
    LLM-judged tier for whatever this function still can't place —
    reusing the exact mechanism `phases/phase1b_validation.py` already
    runs in production for a different reconciliation job, rather than
    inventing a second embedding/LLM matcher here."""
    target = _content_tokens(display_name)
    if not target:
        return None
    best_name, best_score = None, 0.0
    for candidate in candidates:
        cand_tokens = _content_tokens(candidate)
        if not cand_tokens:
            continue
        overlap = len(target & cand_tokens) / len(target | cand_tokens)
        if overlap > best_score:
            best_name, best_score = candidate, overlap
    return best_name if best_score >= _FUZZY_MATCH_THRESHOLD else None


@dataclass
class ConceptGap:
    spread_low: int
    spread_high: int
    probable: List[str] = field(default_factory=list)
    never_addressed: List[str] = field(default_factory=list)
    # name -> why it's never_addressed, distinguishing the two unrelated
    # failure modes this report can produce (see the module docstring):
    # a real citation the census verified, in a chunk no claim's own
    # citation happened to reach ("chunk-coincidence" — worse on large
    # documents), versus the census never verifying a location for this
    # name at all ("no verified citation" — unrelated to document size,
    # a property of the name itself).
    never_addressed_reasons: Dict[str, str] = field(default_factory=dict)
    addressed_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "spread": {"low": self.spread_low, "high": self.spread_high, "probable": self.probable},
            "claims_never_addressed": self.never_addressed,
            "claims_never_addressed_reasons": self.never_addressed_reasons,
            "addressed_count": self.addressed_count,
        }


@dataclass
class GapReport:
    per_concept: Dict[str, ConceptGap] = field(default_factory=dict)
    structural_coverage: Dict[str, Any] = field(default_factory=dict)
    ran: bool = True
    skipped_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ran": self.ran,
            "skipped_reason": self.skipped_reason,
            "per_concept": {k: v.to_dict() for k, v in self.per_concept.items()},
            "structural_coverage": self.structural_coverage,
        }


def build_gap_report(
    ontology,
    chunks: List[str],
    llm_client,
    claims: List[ResolvedClaim],
    source_text: str = "",
    runs: int = 3,
    max_chunks: int = 200,
    force: bool = False,
    db_session=None,
) -> GapReport:
    """The census-vs-claims diff.

    Gated by `max_chunks` the same way the source repo's own build-time census
    is gated by `census_max_chunks` — the census reads every chunk `runs`
    times plus one extra `census_many` pass for chunk locations (`CensusSpread`
    doesn't carry per-run locations, by design, to save memory across `runs`),
    so this is real, visible cost, not something to run unconditionally.

    `db_session`, when supplied, is passed through to `census_repeated` so
    `census_batch` resolves through the database-backed settings registry
    (`phases/settings_registry.py`) instead of always taking the built-in
    default — the same "database, then built-in default" resolution every
    other tunable in this codebase already goes through, now actually
    reachable from this entry point.
    """
    if len(chunks) > max_chunks and not force:
        return GapReport(
            ran=False,
            skipped_reason=(
                f"{len(chunks)} chunks exceeds the {max_chunks}-chunk gap-report limit "
                f"({runs} census reads + 1 location pass = {(runs + 1) * len(chunks)} "
                f"chunk-reads). Pass force=true to run it anyway."
            ),
        )

    concepts: List[Tuple[str, str]] = [
        (ct.name, ct.description) for ct in ontology.concept_types
    ]
    descriptions: Dict[str, str] = dict(concepts)

    spreads = census_repeated(concepts, chunks, llm_client, runs=runs, db_session=db_session)
    located = census_many(concepts, chunks, llm_client, db_session=db_session)

    touched_chunks: Set[int] = {c for claim in claims for c in claim.source_chunks}

    per_concept: Dict[str, ConceptGap] = {}
    for name, spread in spreads.items():
        result = located.get(name)
        chunk_of = result.chunk_of if result else {}
        # Every chunk each slug was independently confirmed in, not just the
        # one chunk_of keeps — issue #11. Consulted only as a fallback below,
        # once the primary chunk (chunk_of) fails to explain a claim's own
        # citation.
        chunks_of = result.chunks_of if result else {}
        result_names = result.names if result else []
        probable = spread.probable

        # Tier 1 (exact) + tier 2 (free lexical fuzzy match) — unchanged from
        # before issue #2's fix. Whatever's still unplaced after these two
        # goes to `residual` for a tier-3 attempt below, rather than being
        # concluded unaddressed immediately.
        located_chunk_of: Dict[str, Optional[int]] = {}
        located_chunks_of: Dict[str, List[int]] = {}
        matched_via: Dict[str, str] = {}
        residual: List[str] = []
        for display_name in probable:
            located_chunk = chunk_of.get(slugify(display_name))
            if located_chunk is not None:
                located_chunk_of[display_name] = located_chunk
                located_chunks_of[display_name] = chunks_of.get(slugify(display_name), [])
                continue
            fuzzy_hit = _best_fuzzy_match(display_name, result_names)
            if fuzzy_hit is not None:
                located_chunk_of[display_name] = chunk_of.get(slugify(fuzzy_hit))
                located_chunks_of[display_name] = chunks_of.get(slugify(fuzzy_hit), [])
                matched_via[display_name] = fuzzy_hit
                continue
            residual.append(display_name)

        # Tier 3 — issue #2's fix. Only for names the two free tiers above
        # gave up on; only as many LLM calls as `phases.name_reconciliation`
        # itself needs to batch through the residual list (its own layers 1
        # and 2 run first and are usually free too — this rarely reaches an
        # actual model call). A failure here can only ever leave a name
        # unresolved — exactly today's behaviour — never worse and never a
        # crash, mirroring the same try/except precedent already proven at
        # phases/phase1b_validation.py's own reconciliation call site.
        ambiguous_candidates: Dict[str, List[str]] = {}
        if residual and result_names:
            try:
                recon = reconcile_names(
                    sampled_names=result_names, census_names=residual,
                    concept=name, description=descriptions.get(name, ""),
                    llm_client=llm_client,
                )
            except Exception as e:
                logger.warning(
                    f"[GapReport] Name reconciliation failed for {name!r} ({e}); "
                    f"{len(residual)} name(s) stay unresolved, as they would "
                    f"have without this fallback"
                )
                recon = None
            if recon is not None:
                for display_name, matched in recon.matched.items():
                    located_chunk_of[display_name] = chunk_of.get(slugify(matched))
                    located_chunks_of[display_name] = chunks_of.get(slugify(matched), [])
                    matched_via[display_name] = matched
                for display_name, candidates in recon.ambiguous:
                    ambiguous_candidates[display_name] = candidates

        never_addressed = []
        never_addressed_reasons: Dict[str, str] = {}
        for display_name in probable:
            located_chunk = located_chunk_of.get(display_name)
            if located_chunk in touched_chunks:
                continue
            # Issue #11: the primary chunk missed, but the census may have
            # independently confirmed this same instance in another chunk
            # too — each document batch is read with no memory of prior
            # batches, so a concept mentioned again later genuinely gets
            # re-identified, not just repeated. If any of those other
            # confirmed sightings is one a claim actually cited, this is
            # addressed — just not via the chunk chunk_of happened to keep.
            if any(c in touched_chunks for c in located_chunks_of.get(display_name, [])):
                continue
            never_addressed.append(display_name)
            if display_name in ambiguous_candidates:
                candidates = ", ".join(repr(c) for c in ambiguous_candidates[display_name])
                never_addressed_reasons[display_name] = (
                    f"ambiguous — the census likely found this under a different "
                    f"name ({candidates}) but couldn't confirm the pairing with "
                    f"confidence, so it isn't counted as addressed"
                )
            elif located_chunk is None:
                never_addressed_reasons[display_name] = (
                    "no verified citation — the census never confirmed a location "
                    "for this name (or a close paraphrase of it), independent of "
                    "what any claim cited"
                )
            else:
                matched_name = matched_via.get(display_name, display_name)
                paraphrase_note = (
                    f" (matched via the census's own paraphrase {matched_name!r})"
                    if matched_name != display_name else ""
                )
                never_addressed_reasons[display_name] = (
                    f"census cites chunk {located_chunk}{paraphrase_note}; no "
                    f"claim's own citation reached it"
                )

        per_concept[name] = ConceptGap(
            spread_low=spread.low,
            spread_high=spread.high,
            probable=probable,
            never_addressed=never_addressed,
            never_addressed_reasons=never_addressed_reasons,
            addressed_count=len(probable) - len(never_addressed),
        )

    structural: Dict[str, Any] = {}
    if source_text:
        # Sections the *claims* touch, not the sections the original
        # extraction consulted — a different question, answered with the
        # same ontology-agnostic pure function.
        sections = document_sections(source_text)
        read_count = sections_read(sections, chunks, sorted(touched_chunks), source_text)
        structural = {"sections_read": read_count, "sections_total": len(sections)}

    return GapReport(per_concept=per_concept, structural_coverage=structural)
