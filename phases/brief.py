"""The brief — an optional human-written note about what a document contains.

Pass A decides what the ontology will contain by reading a **blind slice** of the
document: the first 3,000 characters plus 3,000 from the middle. On prose that is
usually representative. On a machine-generated spec it is not — measured on the
GitHub OpenAPI file, the head slice is licence text and contact details and the
middle slice is decontextualised JSON Schema fragments (`"type": "string"`,
`"html_url"`, `"login"`). Not one endpoint appears in it. Three of the eight
concepts that run discovered were about JSON plumbing, because that is what the
slice happened to contain.

A brief replaces that accident with intent. By default it does two things only:
says what the document is, and names what to leave out. That restraint is
measured, not cautious — see `Brief` below. Sections that *propose* concept
types are opt-in, because when tested they made extraction worse.

**What a brief does not do: it does not improve coverage.** Retrieval still
consults the same fixed number of chunks. A brief changes *which* chunks, not
how many. Better aim, not more shots.

**A brief is guidance, never evidence.** By default it is not indexed and cannot
be cited, because a claim traced back to someone's summary rather than to the
source silently breaks the thing provenance exists for. Indexing it is an
explicit opt-in, and then it is a separately named source.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Section headings are matched on keywords rather than exact text, so a brief
# stays a document a person writes rather than a form they fill in.
_SECTION_PATTERNS = {
    "what_it_is": r"what\s+(this|the)\s+document\s+is|about\s+this|overview",
    "matters": r"what\s+matters|matters\s+to\s+us|concepts?|what\s+to\s+(extract|capture)",
    "ignore": r"what\s+to\s+ignore|ignore|not\s+relevant|exclude",
    "vocabulary": r"words?|vocabular|terms?|phrasing|language",
    "gaps": r"known\s+gaps?|not\s+covered|does\s+not\s+(state|say|cover)|limitations?",
}


MODE_BASIC = "basic"
MODE_FULL = "full"


@dataclass
class Brief:
    """A parsed brief. Every section is optional; the raw text always survives.

    **A brief may describe and subtract, but not propose.**

    That rule comes from measurement, not taste. A hand-written brief for the
    GitHub spec (2026-08-14) was tested against the same run without one:

      * "What to ignore" worked — `data_field` and `data_schema` disappeared,
        freeing two concept slots that had gone to JSON plumbing.
      * "Words this document uses" backfired — the phrase "path template",
        written in backticks to describe an *attribute*, became a concept type
        that then collected example URLs out of schema documentation.
      * "What matters" backfired — splitting one idea across `operation`,
        `path_template` and `scope` left two of them empty and the third
        collecting the wrong things.

    Net: instances fell 38 to 20, real endpoints 4 to 0, structure 1.0 to 0.90.

    The sections that *describe* or *remove* are safe: their worst case is
    dropping something, which shows up as a missing concept. The sections that
    *propose* can invent a concept, which shows up as nothing at all until
    someone reads the output.

    So `basic` mode — the default — uses only the safe half. `full` mode enables
    the rest and has to be asked for explicitly.
    """

    raw: str = ""
    sections: Dict[str, str] = field(default_factory=dict)
    mode: str = MODE_BASIC

    @property
    def is_empty(self) -> bool:
        return not self.raw.strip()

    @property
    def is_full(self) -> bool:
        return self.mode == MODE_FULL

    def risky_sections_present(self) -> List[str]:
        """Proposing sections that are being ignored in basic mode.

        Reported rather than silently dropped: someone who wrote a vocabulary
        section deserves to be told it is not in use.
        """
        if self.is_full:
            return []
        return [name for name in ("matters", "vocabulary") if self.sections.get(name)]

    def guidance(self) -> str:
        """The text shown to pass A.

        In basic mode this is orientation only — what the document is. It cannot
        propose a concept type, so it cannot invent one.
        """
        if self.is_full:
            return self.raw.strip()

        described = self.sections.get("what_it_is", "").strip()
        if described:
            return described
        # A brief with no recognised headings is treated as orientation prose,
        # which is what someone writing a couple of sentences intends.
        if not self.sections:
            return self.raw.strip()
        return ""

    def vocabulary(self) -> List[str]:
        """Terms from the vocabulary section, as a hint for pass A.

        Empty in basic mode. This is the section that produced `path_template`:
        a term written in backticks to describe an attribute was read as a kind
        of thing. Also deliberately never merged mechanically into retrieval
        probes — adding terms to a probe can displace what it would otherwise
        have found (the stargazers case in todo/04).
        """
        if not self.is_full:
            return []

        text = self.sections.get("vocabulary", "")
        if not text:
            return []

        terms: List[str] = []
        # Backticked and quoted terms are the explicit ones.
        terms.extend(re.findall(r"`([^`]{2,60})`", text))
        terms.extend(re.findall(r'"([^"]{2,60})"', text))
        # Bold terms too — a common way to write these.
        terms.extend(re.findall(r"\*\*([^*]{2,60})\*\*", text))

        cleaned = []
        for term in terms:
            term = term.strip().strip(".,;:")
            if term and term.lower() not in {t.lower() for t in cleaned}:
                cleaned.append(term)
        return cleaned

    def ignore_terms(self) -> List[str]:
        """Concept names the brief asks to leave out.

        Active in every mode. Subtractive, so its worst case is a missing
        concept — visible in the output and in the structure score — rather than
        an invented one. It is also the part that measurably worked.
        """
        text = self.sections.get("ignore", "")
        if not text:
            return []
        terms = re.findall(r"`([^`]{2,60})`", text) + re.findall(r"\*\*([^*]{2,60})\*\*", text)
        return [t.strip().lower() for t in terms if t.strip()]

    def known_gaps(self) -> List[str]:
        """Things the brief says the document does not cover.

        Seeded into the checklist as open items. They are still classified
        against the document before anyone is asked about them — the brief is a
        person's belief about the document, not a finding.
        """
        text = self.sections.get("gaps", "")
        if not text:
            return []

        gaps = []
        for line in text.splitlines():
            line = line.strip().lstrip("-*• ").strip()
            if len(line) > 12:
                gaps.append(line)
        return gaps

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw": self.raw,
            "sections": self.sections,
            "mode": self.mode,
            "vocabulary": self.vocabulary(),
            "ignore_terms": self.ignore_terms(),
            "known_gaps": self.known_gaps(),
            "risky_sections_ignored": self.risky_sections_present(),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Brief":
        if not data:
            return cls()
        return cls(
            raw=data.get("raw", ""),
            sections=data.get("sections", {}) or {},
            mode=data.get("mode", MODE_BASIC),
        )


def parse_brief(text: str, mode: str = MODE_BASIC) -> Brief:
    """Parse a markdown brief into named sections.

    Unrecognised headings are kept in the raw text and simply not indexed by
    name — a brief with no headings at all still works as guidance, which is the
    point. Nothing here should make a person feel they have filled in a form
    wrongly.
    """
    if not text or not text.strip():
        return Brief(mode=mode)

    mode = mode if mode in (MODE_BASIC, MODE_FULL) else MODE_BASIC
    brief = Brief(raw=text, mode=mode)

    # Split on markdown headings of any level.
    parts = re.split(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", text, flags=re.MULTILINE)
    # parts = [preamble, heading1, body1, heading2, body2, ...]
    for i in range(1, len(parts) - 1, 2):
        heading, body = parts[i].strip(), parts[i + 1].strip()
        for name, pattern in _SECTION_PATTERNS.items():
            if re.search(pattern, heading, re.IGNORECASE):
                # First match wins, so a later loose heading cannot overwrite an
                # earlier explicit one.
                brief.sections.setdefault(name, body)
                break

    ignored = brief.risky_sections_present()
    logger.info(
        f"[Brief] Parsed {len(brief.sections)} section(s) in {brief.mode} mode: "
        f"{sorted(brief.sections)}; {len(brief.ignore_terms())} ignore term(s), "
        f"{len(brief.known_gaps())} known gap(s)"
    )
    if ignored:
        logger.warning(
            f"[Brief] Ignoring {ignored} — these sections propose concept types, which "
            f"is the half that backfired when measured. Ask for mode='full' to enable them."
        )
    return brief


def load_brief(path: str, mode: str = MODE_BASIC) -> Brief:
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"No brief at {path}")
    return parse_brief(p.read_text(), mode=mode)
