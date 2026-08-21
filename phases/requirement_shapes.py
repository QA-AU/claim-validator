"""Does a generated requirement have the shape a requirement needs?

Phase 1 asks this of ontology instances; this asks it of requirements, through
the same machinery in `phases.shape_check`. Free, deterministic, and reporting
only — no model call, and nothing is deleted on the strength of a violation.

### What it is not

It is not the quality evaluator, which scores prose, and it is not the
entailment judge, which asks whether the claim is true. Those both cost model
calls and both answer harder questions. This one catches the requirement that
cannot be *acted on* whatever its prose: no acceptance criteria, so nothing to
check; no subject, so nothing to check it against; an id that does not follow
the format the process declared, so it cannot be referenced.

A requirement can be well written, entirely true, and still useless because it
states no criteria. That is the gap this fills, and it costs nothing.

### Rules are data

`requirement_rules` in a profile, alongside the `shape_rules` Phase 1 uses. A
domain that means something different by "requirement" — a clinical checklist
item, a contractual obligation — adjusts the file rather than this module. The
defaults below apply when a profile declares none, because a requirement with no
criteria is defective in every domain.
"""

import logging
import re
from typing import Any, Dict, Optional

from phases.shape_check import ShapeReport, check, record

logger = logging.getLogger(__name__)

# Applied when a profile declares no rules of its own. Deliberately minimal:
# these are the properties without which a requirement cannot be used at all,
# not a house style.
DEFAULT_RULES: Dict[str, Dict[str, Any]] = {
    "requirement": {
        "description": "a requirement needs a subject, a title and at least one criterion",
        "require_fields": ["title"],
        "require_any_of": ["criteria", "expected_behavior"],
        "require_subject": True,
    }
}


def _evaluate(requirement, rule: Dict[str, Any]) -> Optional[str]:
    """The first reason this requirement cannot be acted on, or None."""
    for field_name in rule.get("require_fields") or []:
        if not str(getattr(requirement, field_name, "") or "").strip():
            return f"no {field_name}"

    alternatives = rule.get("require_any_of") or []
    if alternatives:
        # Any-of, not all-of. An earlier version of the Phase 1 rules treated
        # alternatives as all required and flagged every valid instance.
        satisfied = any(
            (getattr(requirement, name, None) or "") if not isinstance(
                getattr(requirement, name, None), list
            ) else getattr(requirement, name)
            for name in alternatives
        )
        if not satisfied:
            return f"states nothing checkable — none of {', '.join(alternatives)}"

    subject = str(getattr(requirement, "endpoint", "") or "").strip()
    if rule.get("require_subject") and not subject:
        return "no subject — nothing to check this against"

    # A present subject is not necessarily a valid one. Tested against a real
    # defective ontology: Phase 1 filed 19 OpenAPI tags as endpoints, Phase 2
    # wrote well-formed requirements about them, and every check passed because
    # the subject was merely non-empty. `projects` is a tag; `GET /projects` is
    # an operation. The domain already declares which is which for Phase 1, so
    # the same pattern is applied here.
    pattern = rule.get("subject_pattern")
    if subject and pattern and not re.search(pattern, subject):
        return (
            f"subject {subject!r} does not look like something you can test against "
            f"— it names a thing, not an operation on it"
        )

    pattern = rule.get("id_pattern")
    if pattern and not re.search(pattern, str(getattr(requirement, "id", "") or "")):
        return f"id {getattr(requirement, 'id', '')!r} does not match the declared format"

    return None


def check_requirement_shapes(requirements_set, profile=None, tracker=None) -> ShapeReport:
    """Check every requirement, record the findings, return the report.

    Runs unconditionally. Phase 1's equivalent was once gated on an unrelated
    condition and therefore skipped on the command line; this has no such guard,
    and falls back to `DEFAULT_RULES` rather than checking nothing when a profile
    declares no rules of its own.
    """
    rules = (getattr(profile, "requirement_rules", None) or {}) if profile else {}
    rules = rules or DEFAULT_RULES

    report = check(
        requirements_set.requirements,
        rules=rules,
        # Every requirement is governed by the same single rule key; the phase
        # has no per-item kinds the way an ontology has concepts.
        kind_of=lambda r: "requirement",
        phase="phase2",
        identify=lambda r: str(getattr(r, "id", "") or ""),
        name_of=lambda r: str(getattr(r, "title", "") or ""),
        evaluate=_evaluate,
    )

    if report.violations:
        report.messages = [
            f"{len(report.violations)} of {report.checked} requirement(s) cannot be acted "
            f"on as written: "
            + "; ".join(f"{v.item_id or v.name} ({v.reason})" for v in report.violations[:3])
            + (f", and {len(report.violations) - 3} more" if len(report.violations) > 3 else "")
        ]

    return record(report, tracker)
