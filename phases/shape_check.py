"""Shape checking as one repeatable process, usable by any phase.

Every phase produces items that are supposed to look like something: Phase 1
produces ontology instances that should look like the concept they were filed
under, Phase 2 produces requirements that should look like requirements. The
question is the same in both cases — *does this item have the shape its kind
demands?* — so the machinery is written once here and each phase supplies the
rules and the accessors.

### Why this is not left to each phase

It was, and the result was that Phase 1's shape check ran only when an ontology
store happened to be in use. The check is free and deterministic; gating it on
an unrelated concern meant a plain command-line extraction received no checking
at all. Rules that live in one place, run from one function, are much harder to
skip by accident.

### Free, deterministic, and reporting only

No model call. The rules are data (`profiles/*.json`), so a new domain adds a
file rather than a branch. Nothing is ever deleted or relabelled on the strength
of a violation: a false positive that silently removed a real item would be
worse than the error it catches, so a violation is a flag for a person.

### Every violation is recorded

Findings go to the run tracker, and therefore to the database, rather than only
into a response that a browser may never see. A check whose findings exist only
in a terminal is a check nobody can audit later.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Violation:
    """One item that does not have the shape its kind requires."""

    item_id: str
    kind: str
    name: str
    reason: str
    severity: str = "warning"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "name": self.name,
            "reason": self.reason,
            "severity": self.severity,
        }


@dataclass
class ShapeReport:
    """What a shape check found, and what it looked at.

    `checked` matters as much as the violations: zero violations out of zero
    items checked is not a clean bill of health, and the two must be
    distinguishable by any caller.
    """

    phase: str = ""
    checked: int = 0
    violations: List[Violation] = field(default_factory=list)
    rules_applied: List[str] = field(default_factory=list)
    # Wording supplied by the phase. A generic message can say "3 items are
    # mis-shaped"; only the phase can say "3 instances filed as endpoints do not
    # look like one, and here is why". The specific version is more useful to a
    # reader, so it wins where a phase offers one.
    messages: List[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.violations

    @property
    def ran(self) -> bool:
        """Whether any rule was actually applied.

        A profile with no rules for these items produces a report that is empty
        because nothing was checked, which is a different statement from
        everything having passed.
        """
        return bool(self.rules_applied)

    def review_flags(self) -> List[str]:
        if not self.violations:
            return []
        if self.messages:
            return list(self.messages)
        shown = "; ".join(f"{v.name or v.item_id} ({v.reason})" for v in self.violations[:3])
        more = f", and {len(self.violations) - 3} more" if len(self.violations) > 3 else ""
        return [
            f"{len(self.violations)} of {self.checked} item(s) do not have the shape their "
            f"kind requires: {shown}{more}"
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "checked": self.checked,
            "ran": self.ran,
            "clean": self.clean,
            "rules_applied": self.rules_applied,
            "violation_count": len(self.violations),
            "violations": [v.to_dict() for v in self.violations],
            "review_flags": self.review_flags(),
        }


def record(report: ShapeReport, tracker=None) -> ShapeReport:
    """Send a report's findings to the run tracker, and so to the database.

    Each violation is its own event rather than one lumped summary, because a
    row per finding is what makes them queryable afterwards — "which runs
    produced endpoints with no HTTP method" is a question about individual
    violations, not about counts.
    """
    if tracker is None:
        return report

    tracker.event(
        "shape_check",
        report.phase,
        {
            "checked": report.checked,
            "violations": len(report.violations),
            "rules_applied": report.rules_applied,
            "ran": report.ran,
        },
        severity="warning" if report.violations else "info",
    )

    for violation in report.violations:
        tracker.event("shape_violation", report.phase, violation.to_dict(), severity="warning")

    for flag in report.review_flags():
        tracker.event("review_flag", report.phase, {"message": flag}, severity="warning")

    return report


def check(
    items: Iterable[Any],
    rules: Dict[str, Dict[str, Any]],
    kind_of: Callable[[Any], Optional[str]],
    phase: str = "",
    identify: Optional[Callable[[Any], str]] = None,
    name_of: Optional[Callable[[Any], str]] = None,
    skip: Optional[Callable[[Any], bool]] = None,
    evaluate: Optional[Callable[[Any, Dict[str, Any]], Optional[str]]] = None,
) -> ShapeReport:
    """Check items against the rules for their kind.

    `kind_of` maps an item to the rule key that governs it; `evaluate` returns a
    reason string when the item fails, or None when it passes. Everything else
    is presentation, so a phase supplies only what is genuinely phase-specific.
    """
    report = ShapeReport(phase=phase)
    if not rules:
        return report

    identify = identify or (lambda i: str(getattr(i, "id", "") or ""))
    name_of = name_of or (lambda i: str(getattr(i, "name", "") or ""))

    applied = set()
    for item in items:
        if skip and skip(item):
            continue

        kind = kind_of(item)
        rule = rules.get(kind) if kind else None
        if not rule:
            continue

        applied.add(kind)
        report.checked += 1

        reason = evaluate(item, rule) if evaluate else None
        if reason:
            report.violations.append(
                Violation(
                    item_id=identify(item),
                    kind=kind,
                    name=name_of(item),
                    reason=reason,
                )
            )

    report.rules_applied = sorted(applied)
    if report.violations:
        logger.warning(
            f"[ShapeCheck:{phase}] {len(report.violations)} of {report.checked} item(s) "
            f"do not match their declared shape"
        )
    return report
