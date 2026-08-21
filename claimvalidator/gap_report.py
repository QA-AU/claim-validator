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

from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Tuple

from phases.census import census_many, census_repeated
from phases.completeness import document_sections, sections_read
from phases.phase1_models import slugify

from claimvalidator.claim_shims import ResolvedClaim


@dataclass
class ConceptGap:
    spread_low: int
    spread_high: int
    probable: List[str] = field(default_factory=list)
    never_addressed: List[str] = field(default_factory=list)
    addressed_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "spread": {"low": self.spread_low, "high": self.spread_high, "probable": self.probable},
            "claims_never_addressed": self.never_addressed,
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
) -> GapReport:
    """The census-vs-claims diff.

    Gated by `max_chunks` the same way the source repo's own build-time census
    is gated by `census_max_chunks` — the census reads every chunk `runs`
    times plus one extra `census_many` pass for chunk locations (`CensusSpread`
    doesn't carry per-run locations, by design, to save memory across `runs`),
    so this is real, visible cost, not something to run unconditionally.
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

    spreads = census_repeated(concepts, chunks, llm_client, runs=runs)
    located = census_many(concepts, chunks, llm_client)

    touched_chunks: Set[int] = {c for claim in claims for c in claim.source_chunks}

    per_concept: Dict[str, ConceptGap] = {}
    for name, spread in spreads.items():
        result = located.get(name)
        chunk_of = result.chunk_of if result else {}
        probable = spread.probable

        never_addressed = [
            display_name for display_name in probable
            if chunk_of.get(slugify(display_name)) not in touched_chunks
        ]
        per_concept[name] = ConceptGap(
            spread_low=spread.low,
            spread_high=spread.high,
            probable=probable,
            never_addressed=never_addressed,
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
