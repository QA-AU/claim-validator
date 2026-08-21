"""Bare `id + text` claims, reframed as whatever shape the reused check-modules
expect — the same pattern `phases/entailment.py` already uses internally for
this purpose (see `_Reframed` in `recheck_against_better_passages`), applied
to input from outside the pipeline instead of the pipeline's own output.

Neither `check_requirement_shapes` nor `judge_entailment` was changed to
support this — both already accept duck-typed objects; these are the objects.
"""

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

# A claim shorter than this has nothing a judge could check. Below the shape
# check's own "states nothing checkable" wording — not a new violation type.
MIN_CLAIM_CHARS = 12

# The built-in default. A caller may override per job (see resolve_shape_rules)
# for a specialist task where "well-formed" means something else — a named
# subject required, a longer minimum, additional fields — without touching
# this file or the reused `requirement_shapes.py`.
BARE_CLAIM_RULES: Dict[str, Dict[str, Any]] = {
    "requirement": {
        "description": "a claim needs non-trivial, checkable text",
        "require_fields": ["title"],
        "require_any_of": ["expected_behavior"],
        "require_subject": False,
    }
}


def resolve_shape_rules(overrides: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The default, or the default with a caller's overrides merged in.

    Merged rather than replaced: a caller wanting only `require_subject: True`
    for a specialist task shouldn't also have to restate `require_fields` and
    `require_any_of` to avoid silently losing them.
    """
    if not overrides:
        return BARE_CLAIM_RULES
    merged = {**BARE_CLAIM_RULES["requirement"], **overrides}
    return {"requirement": merged}


@dataclass
class ResolvedClaim:
    """One input claim, after retrieval has (or hasn't) found it a citation."""

    id: str
    text: str
    source_chunks: List[int] = field(default_factory=list)
    had_citation: bool = False  # retrieval was skipped — a citation was pre-supplied


class _ShapeClaim:
    """Feeds `check_requirement_shapes` — needs `id`, `title`, and whichever
    of `criteria`/`expected_behavior` the active rules ask for."""

    def __init__(self, claim: ResolvedClaim):
        self.id = claim.id
        # `require_fields: ["title"]` only checks non-empty, so the id (always
        # present) satisfies it — the actual "is there anything to judge" check
        # is `require_any_of: ["expected_behavior"]`, driven by MIN_CLAIM_CHARS.
        self.title = claim.id or "claim"
        text = (claim.text or "").strip()
        self.expected_behavior = text if len(text) >= MIN_CLAIM_CHARS else ""
        self.criteria: List[str] = []


class _ClaimSet:
    """The `.requirements` wrapper `check_requirement_shapes` expects."""

    def __init__(self, claims: List[ResolvedClaim]):
        self.requirements = [_ShapeClaim(c) for c in claims]


class _JudgeClaim:
    """Feeds `judge_entailment` — needs `id`, `title`, `expected_behavior`,
    `criteria`, `source_chunks`. `title` carries the verbatim claim text,
    since `_claim_of()` in entailment.py renders `title` first and this is
    the one thing that must reach the judge unparaphrased."""

    def __init__(self, claim: ResolvedClaim):
        self.id = claim.id
        self.title = claim.text
        self.expected_behavior = ""
        self.criteria: List[str] = []
        self.source_chunks = claim.source_chunks


def shape_profile(overrides: Optional[Dict[str, Any]] = None) -> SimpleNamespace:
    return SimpleNamespace(requirement_rules=resolve_shape_rules(overrides))
