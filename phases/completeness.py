"""How much of the document did the ontology actually capture?

Coverage answers a narrower question than it looks: chunk reach is how much of
the document retrieval *touched*, not how much of its content was *captured*.
On the GitHub spec those happened to track closely — 0.48% reach against 0.33%
of operations extracted — but that is a property of that document, not a
guarantee, and nothing in the pipeline said "4 of 1,220".

This module says it, at three confidence levels, and never blurs them:

**Exact, by parsing.** If the document parses as structured data, the totals are
facts: an OpenAPI file has 1,220 operations and there is nothing to estimate.

**Exact, by structure.** Prose is not unstructured — contracts number their
clauses, protocols and reports use headings. Those are countable, and so is how
many of them a run actually read. "Read 3 of 7 clauses" is a *count*. It is a
weaker claim than counting instances, and worded as what it is: sections
reached, not what those sections contain.

**Estimated.** Only when neither applies: per concept, how many chunks in the
whole index contain its own surface terms, against how many were read.
"`endpoint` appears in 3,412 chunks; extraction read 8 of them" is not a count
of endpoints and is never presented as one.

The distinction is the point. An estimate presented as a count is the same
failure this whole project has been removing, one level up — which is also why
the middle tier exists at all. "There is no way to count prose" was too
pessimistic, and settling for an estimate where a count was available would have
been the same mistake in a quieter form.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Below this share of an exactly-known total, the ontology is a sample of that
# kind of thing rather than a description of it. Same spirit as the coverage
# threshold, and the same reason for being generous: sampling is accepted, it
# just may not be mistaken for completeness.
LOW_CAPTURE = 0.5

# What counts as thin, and what counts as a term too common to be evidence.
# Both are judgement calls that a run should be able to state rather than imply.
SETTINGS_PROCESS = "completeness"
DEFAULT_SETTINGS = {
    "low_capture": 0.5,
    "undiscriminating": 0.5,
    "min_sections": 3,
}

# Concepts whose surface terms appear in more than this share of the index are
# not discriminating — "path" in an OpenAPI file matches nearly everything — so
# the per-concept figure is reported without a flag.
UNDISCRIMINATING = 0.5


@dataclass
class ExactTotal:
    """A count taken from the document itself, with no model and no guessing."""

    bucket: str
    total: int
    captured: int
    source: str = ""  # how the total was obtained, e.g. "openapi paths"
    # Carried on the record so a figure states the threshold it was judged
    # against; defaults to the constant, so nothing that ignores it changes.
    low_capture: float = LOW_CAPTURE

    @property
    def ratio(self) -> float:
        return self.captured / self.total if self.total else 0.0

    @property
    def is_low(self) -> bool:
        return bool(self.total) and self.ratio < self.low_capture

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bucket": self.bucket,
            "total": self.total,
            "captured": self.captured,
            "ratio": round(self.ratio, 4),
            "is_low": self.is_low,
            "source": self.source,
            "kind": "exact",
        }


@dataclass
class ConceptReach:
    """How much of a concept's own material was read. An estimate, labelled as one."""

    concept: str
    matching_chunks: int
    consulted_chunks: int
    instances_found: int
    total_chunks: int = 0
    low_capture: float = LOW_CAPTURE
    undiscriminating: float = UNDISCRIMINATING

    @property
    def ratio(self) -> float:
        return self.consulted_chunks / self.matching_chunks if self.matching_chunks else 0.0

    @property
    def discriminating(self) -> bool:
        """False when the terms match most of the document and say little."""
        if not self.total_chunks or not self.matching_chunks:
            return True
        return (self.matching_chunks / self.total_chunks) < self.undiscriminating

    @property
    def is_low(self) -> bool:
        return self.discriminating and bool(self.matching_chunks) and self.ratio < self.low_capture

    def to_dict(self) -> Dict[str, Any]:
        return {
            "concept": self.concept,
            "matching_chunks": self.matching_chunks,
            "consulted_chunks": self.consulted_chunks,
            "instances_found": self.instances_found,
            "ratio": round(self.ratio, 4),
            "discriminating": self.discriminating,
            "is_low": self.is_low,
            "kind": "estimate",
        }


@dataclass
class CompletenessReport:
    exact: List[ExactTotal] = field(default_factory=list)
    estimated: List[ConceptReach] = field(default_factory=list)
    document_format: str = "unknown"

    @property
    def has_exact(self) -> bool:
        return bool(self.exact)

    def review_flags(self) -> List[str]:
        """What is worth saying out loud. Exact findings are worded as facts."""
        flags = []

        for total in self.exact:
            if not total.is_low:
                continue
            if total.bucket == "sections":
                flags.append(
                    f"Read {total.captured} of {total.total} sections of the document "
                    f"({total.ratio:.0%}) — an exact count of sections reached, not of "
                    f"what they contain"
                )
            else:
                flags.append(
                    f"Captured {total.captured} of {total.total} {total.bucket} "
                    f"({total.ratio:.1%}) — counted directly from the document, not estimated"
                )

        low = [e for e in self.estimated if e.is_low]
        if low:
            worst = min(low, key=lambda e: e.ratio)
            flags.append(
                f"{len(low)} concept(s) were read from a fraction of the material "
                f"mentioning them — worst is {worst.concept!r} at "
                f"{worst.consulted_chunks} of {worst.matching_chunks} chunks "
                f"({worst.ratio:.1%}). An estimate from term matching, not a count"
            )

        return flags

    @property
    def needs_review(self) -> bool:
        return bool(self.review_flags())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_format": self.document_format,
            "has_exact_totals": self.has_exact,
            "exact": [e.to_dict() for e in self.exact],
            "estimated": [e.to_dict() for e in self.estimated],
            "review_flags": self.review_flags(),
            "needs_review": self.needs_review,
            # Said plainly so a consumer never treats the second list as counts.
            "note": (
                "`exact` totals are parsed from the document and are facts. "
                "`estimated` figures come from matching each concept's surface terms "
                "across the index; they measure unread material, not instance counts."
            ),
        }


# ---------------------------------------------------------------------------
# Exact — parse the document
# ---------------------------------------------------------------------------


def exact_totals(text: str) -> Dict[str, Any]:
    """Counts taken straight from a structured document.

    Returns `{}` for anything that does not parse, which is the honest answer —
    an absent exact total is better than an invented one.
    """
    stripped = (text or "").strip()
    if not stripped.startswith(("{", "[")):
        return {}

    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return {}

    if not isinstance(data, dict):
        return {}

    if "openapi" not in data and "swagger" not in data:
        return {"format": "json"}

    methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    paths = data.get("paths") or {}
    operations = sum(
        1
        for item in paths.values()
        if isinstance(item, dict)
        for method in item
        if method.lower() in methods
    )
    tags = [t.get("name") for t in (data.get("tags") or []) if isinstance(t, dict)]
    schemas = (data.get("components") or {}).get("schemas") or {}

    return {
        "format": "openapi",
        "operations": operations,
        "paths": len(paths),
        "tags": len(tags),
        "schemas": len(schemas),
    }


# Prose is not unstructured. Contracts number their clauses, protocols and
# reports use headings, specifications use both — and those are countable.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(\S.*)$", re.MULTILINE)
_CLAUSE = re.compile(r"^\s{0,3}(\d{1,2})\.\s+([A-Z].*)$", re.MULTILINE)

# Fewer sections than this and "read 1 of 2" says nothing useful.
MIN_SECTIONS = 3


def document_sections(text: str) -> List[Dict[str, Any]]:
    """Sections of a prose document, with their character offsets.

    Headings first, then numbered clauses. Returns `[]` when the document has
    too little structure to say anything — an absent measure rather than a
    meaningless one.
    """
    if not text:
        return []

    for pattern, kind in ((_HEADING, "headings"), (_CLAUSE, "numbered clauses")):
        matches = list(pattern.finditer(text))
        if len(matches) < MIN_SECTIONS:
            continue

        sections = []
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            sections.append(
                {
                    "title": match.group(len(match.groups())).strip()[:80],
                    "start": match.start(),
                    "end": end,
                    "kind": kind,
                }
            )
        return sections

    return []


def sections_read(
    sections: List[Dict[str, Any]], chunks: List[str], consulted: List[int], text: str
) -> int:
    """How many sections contain at least one chunk the extraction actually read.

    Chunk positions are computed, not searched. `chunk_text` advances by exactly
    `CHUNK_SIZE - CHUNK_OVERLAP` each step, so chunk *i* begins at `i * stride`
    in the source — the only slack is the whitespace `.strip()` removes from the
    ends, which is a character or two.

    Two wrong approaches were tried first, and both are worth naming because
    each looked safer than the arithmetic:

    * `text.find(head)` returns the *first* occurrence, so on repetitive text —
      boilerplate, repeated schema fragments, anything a specification is full
      of — every chunk resolved to the same offset and the measure collapsed to
      one section.
    * Searching within a window around the expected position fixed that but
      introduced its own error: with sections shorter than the window, a chunk
      could resolve into its neighbour and be counted against the wrong section.

    The computed position has neither failure and needs no text scan at all.
    """
    if not sections or not chunks:
        return 0

    from phases.phase1_rag_indexer import CHUNK_OVERLAP, CHUNK_SIZE

    stride = max(CHUNK_SIZE - CHUNK_OVERLAP, 1)

    touched = set()
    for index in consulted:
        if not (0 <= index < len(chunks)):
            continue

        offset = index * stride
        for n, section in enumerate(sections):
            if section["start"] <= offset < section["end"]:
                touched.add(n)
                break
    return len(touched)


def _captured(ontology, bucket_aliases: List[str]) -> int:
    """Instances in whichever concepts map to a bucket."""
    total = 0
    for concept in ontology.concept_types:
        normalised = concept.name.strip().lower().replace(" ", "_").replace("-", "_")
        if any(a in normalised or normalised in a for a in bucket_aliases):
            total += len(concept.instances)
    return total


# ---------------------------------------------------------------------------
# Estimated — how much of each concept's material went unread
# ---------------------------------------------------------------------------


def concept_reach(ontology, chunks: List[str]) -> List[ConceptReach]:
    """Per concept: chunks mentioning it, against chunks actually read.

    Uses the concept's own `surface_terms`, which are the document's wording for
    it — the same probe retrieval used, so this measures the material that probe
    could have reached and did not.
    """
    if not chunks:
        return []

    lowered = [c.lower() for c in chunks]
    out = []

    for concept in ontology.concept_types:
        terms = [t.strip().lower() for t in concept.surface_terms if len(t.strip()) > 2]
        if not terms:
            terms = [concept.name.replace("_", " ").lower()]

        matching = sum(1 for chunk in lowered if any(t in chunk for t in terms))
        consulted = len(set(concept.chunks_consulted))

        out.append(
            ConceptReach(
                concept=concept.name,
                matching_chunks=matching,
                # Cannot have read more of them than exist.
                consulted_chunks=min(consulted, matching) if matching else consulted,
                instances_found=len(concept.instances),
                total_chunks=len(chunks),
            )
        )

    return out


def measure(
    ontology,
    chunks: Optional[List[str]] = None,
    source_text: Optional[str] = None,
    profile=None,
) -> CompletenessReport:
    """Everything knowable about completeness, without a model.

    `source_text` enables exact counts and is worth supplying whenever the
    original document is at hand — the persisted chunk stream cannot be used for
    this, since overlapping chunks make it larger than the original and no
    longer parseable.
    """
    report = CompletenessReport()

    totals = exact_totals(source_text or "")
    report.document_format = totals.get("format", "unknown")

    # bucket -> which parsed total it should be compared against. Data on the
    # profile, so a new domain brings its own mapping.
    mapping = (getattr(profile, "completeness", None) or {}) if profile else {}
    if not mapping and report.document_format == "openapi":
        mapping = {"endpoints": "operations"}

    for bucket, total_key in mapping.items():
        total = totals.get(total_key)
        if not total:
            continue
        aliases = (getattr(profile, "buckets", {}) or {}).get(bucket) or [bucket.rstrip("s")]
        report.exact.append(
            ExactTotal(
                bucket=total_key,
                total=int(total),
                captured=_captured(ontology, [a.lower() for a in aliases]),
                source=f"{report.document_format} {total_key}",
            )
        )

    # Prose has countable structure even when nothing parses. "Read 3 of 7
    # clauses" is an exact count of sections reached — weaker than counting
    # instances, but a count rather than an estimate, which is the distinction
    # this module exists to keep.
    if chunks and source_text:
        sections = document_sections(source_text)
        if sections:
            read = sections_read(
                sections, chunks, sorted(ontology.coverage.chunks_consulted), source_text
            )
            report.exact.append(
                ExactTotal(
                    bucket="sections",
                    total=len(sections),
                    captured=read,
                    source=f"{sections[0]['kind']} — sections containing a chunk that was read",
                )
            )

    if chunks:
        report.estimated = concept_reach(ontology, chunks)

    if report.review_flags():
        logger.warning(f"[Completeness] {'; '.join(report.review_flags())}")
    return report
