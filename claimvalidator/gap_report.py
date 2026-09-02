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

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from phases.census import census_many, census_repeated
from phases.completeness import document_sections, sections_read
from phases.phase1_models import slugify

from claimvalidator.claim_shims import ResolvedClaim

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


def _content_tokens(text: str) -> Set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOPWORDS}


def _best_fuzzy_match(display_name: str, candidates: List[str]) -> Optional[str]:
    """The candidate whose content-word overlap with `display_name` is
    highest, by Jaccard similarity — or None if nothing clears
    `_FUZZY_MATCH_THRESHOLD`. `candidates` is the *other* census pass's own
    independently-generated names for the same concept."""
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

    spreads = census_repeated(concepts, chunks, llm_client, runs=runs, db_session=db_session)
    located = census_many(concepts, chunks, llm_client, db_session=db_session)

    touched_chunks: Set[int] = {c for claim in claims for c in claim.source_chunks}

    per_concept: Dict[str, ConceptGap] = {}
    for name, spread in spreads.items():
        result = located.get(name)
        chunk_of = result.chunk_of if result else {}
        result_names = result.names if result else []
        probable = spread.probable

        never_addressed = []
        never_addressed_reasons: Dict[str, str] = {}
        for display_name in probable:
            matched_name = display_name
            located_chunk = chunk_of.get(slugify(display_name))
            if located_chunk is None:
                # The exact match against census_many's own naming missed —
                # try its content-word overlap with that pass's names before
                # concluding there's truly no verified location (see the
                # module-level comment above _best_fuzzy_match for why this
                # exists: two independent LLM calls rarely name the same
                # instance identically).
                fuzzy_hit = _best_fuzzy_match(display_name, result_names)
                if fuzzy_hit is not None:
                    matched_name = fuzzy_hit
                    located_chunk = chunk_of.get(slugify(fuzzy_hit))
            if located_chunk in touched_chunks:
                continue
            never_addressed.append(display_name)
            if located_chunk is None:
                never_addressed_reasons[display_name] = (
                    "no verified citation — the census never confirmed a location "
                    "for this name (or a close paraphrase of it), independent of "
                    "what any claim cited"
                )
            else:
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
