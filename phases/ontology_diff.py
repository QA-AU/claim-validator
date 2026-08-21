"""Diff two ontology versions — what a re-extraction actually changed.

Re-extraction writes a new version rather than overwriting, which only helps if
you can see what moved. Without a diff, a re-run silently produces a different
ontology and nobody can tell whether the document changed, the extraction
improved, or something quietly went missing.

Instances are matched on their deterministic id (`concept:instance`), not on
position, so a reordered extraction reads as unchanged rather than as a total
rewrite.

Three kinds of change are kept apart on purpose:

  * **content** — a description or attribute differs. The claim changed.
  * **provenance** — the same claim is now cited to a different passage. Common
    and usually harmless, since retrieval need not return the same chunk twice.
  * **coverage** — how much of the document was read. A re-run that reads less
    can look like an improvement (fewer, cleaner concepts) while being a
    regression, so reach is diffed alongside content.

See todo/08-ontology-lifecycle-and-switching.md.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Keys written by the pipeline rather than by the model. They describe where an
# instance came from, not what it says, so they are compared as provenance.
_PROVENANCE_KEYS = {"id", "source", "source_chunk", "source_document",
                    "asserted_by", "asserted_at", "question", "checklist_item"}


@dataclass
class InstanceChange:
    """One instance that exists in both versions but is not identical."""

    instance_id: str
    concept: str
    name: str
    content_changes: Dict[str, Tuple[Any, Any]] = field(default_factory=dict)
    provenance_changes: Dict[str, Tuple[Any, Any]] = field(default_factory=dict)

    @property
    def content_changed(self) -> bool:
        return bool(self.content_changes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.instance_id,
            "concept": self.concept,
            "name": self.name,
            "content_changes": {k: {"before": b, "after": a} for k, (b, a) in self.content_changes.items()},
            "provenance_changes": {
                k: {"before": b, "after": a} for k, (b, a) in self.provenance_changes.items()
            },
        }


@dataclass
class OntologyDiff:
    """The delta between two ontology versions."""

    concepts_added: List[str] = field(default_factory=list)
    concepts_removed: List[str] = field(default_factory=list)
    instances_added: List[str] = field(default_factory=list)
    instances_removed: List[str] = field(default_factory=list)
    instances_changed: List[InstanceChange] = field(default_factory=list)
    coverage_before: Dict[str, Any] = field(default_factory=dict)
    coverage_after: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (
            self.concepts_added
            or self.concepts_removed
            or self.instances_added
            or self.instances_removed
            or any(c.content_changed for c in self.instances_changed)
        )

    @property
    def reach_delta(self) -> float:
        """Change in chunk reach. Negative means the re-run read less of the document."""
        return float(self.coverage_after.get("chunk_reach", 0.0)) - float(
            self.coverage_before.get("chunk_reach", 0.0)
        )

    def review_flags(self) -> List[str]:
        """What about this transition deserves a human look before promotion.

        Concept removal and a drop in reach are the two changes that can make an
        ontology look tidier while making it worse, so they are named explicitly
        rather than left to be inferred from counts.
        """
        flags = []

        if self.concepts_removed:
            flags.append(
                f"{len(self.concepts_removed)} concept type(s) no longer present: "
                f"{', '.join(self.concepts_removed)}"
            )

        if self.instances_removed:
            flags.append(
                f"{len(self.instances_removed)} instance(s) present before and absent now"
            )

        # A tolerance, because reach varies slightly between runs on identical
        # input; only a real reduction is worth raising.
        if self.reach_delta < -0.01:
            flags.append(
                f"Document coverage fell from "
                f"{self.coverage_before.get('chunk_reach', 0.0):.1%} to "
                f"{self.coverage_after.get('chunk_reach', 0.0):.1%} — this version read "
                f"less of the material than the one it replaces"
            )

        return flags

    def summary(self) -> str:
        if self.is_empty:
            return "No content changes."
        parts = []
        if self.concepts_added:
            parts.append(f"+{len(self.concepts_added)} concepts")
        if self.concepts_removed:
            parts.append(f"-{len(self.concepts_removed)} concepts")
        if self.instances_added:
            parts.append(f"+{len(self.instances_added)} instances")
        if self.instances_removed:
            parts.append(f"-{len(self.instances_removed)} instances")
        changed = sum(1 for c in self.instances_changed if c.content_changed)
        if changed:
            parts.append(f"{changed} changed")
        return ", ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "concepts_added": self.concepts_added,
            "concepts_removed": self.concepts_removed,
            "instances_added": self.instances_added,
            "instances_removed": self.instances_removed,
            "instances_changed": [c.to_dict() for c in self.instances_changed],
            "content_changed_count": sum(1 for c in self.instances_changed if c.content_changed),
            "coverage_before": self.coverage_before,
            "coverage_after": self.coverage_after,
            "reach_delta": round(self.reach_delta, 4),
            "review_flags": self.review_flags(),
            "summary": self.summary(),
            "is_empty": self.is_empty,
        }


def _instances_by_id(version: Dict[str, Any]) -> Dict[str, Tuple[str, Dict[str, Any]]]:
    """Map every instance in a serialised ontology to (concept name, payload)."""
    out: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for concept in version.get("concept_types") or []:
        concept_name = concept.get("name", "")
        for instance in concept.get("instances") or []:
            # Versions written before instance ids existed have no `id`; fall
            # back to the same derivation so old versions stay diffable.
            instance_id = instance.get("id") or _fallback_id(concept_name, instance.get("name", ""))
            out[instance_id] = (concept_name, instance)
    return out


def _fallback_id(concept_name: str, instance_name: str) -> str:
    from phases.phase1_models import slugify

    return f"{slugify(concept_name)}:{slugify(instance_name)}"


def _compare_instance(
    instance_id: str, concept: str, before: Dict[str, Any], after: Dict[str, Any]
) -> Optional[InstanceChange]:
    change = InstanceChange(
        instance_id=instance_id, concept=concept, name=after.get("name", before.get("name", ""))
    )

    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if key in _PROVENANCE_KEYS:
            change.provenance_changes[key] = (old, new)
        else:
            change.content_changes[key] = (old, new)

    if not change.content_changes and not change.provenance_changes:
        return None
    return change


def diff_ontologies(before: Dict[str, Any], after: Dict[str, Any]) -> OntologyDiff:
    """Compare two serialised ontology versions.

    Both arguments are the dicts written by `Ontology.to_dict()` — the diff works
    on stored versions, so it can compare any two points in an ontology's history
    without re-running an extraction.
    """
    before = before or {}
    after = after or {}

    before_concepts = {c.get("name", "") for c in (before.get("concept_types") or [])}
    after_concepts = {c.get("name", "") for c in (after.get("concept_types") or [])}

    before_instances = _instances_by_id(before)
    after_instances = _instances_by_id(after)

    changed = []
    for instance_id in sorted(set(before_instances) & set(after_instances)):
        concept, after_payload = after_instances[instance_id]
        change = _compare_instance(
            instance_id, concept, before_instances[instance_id][1], after_payload
        )
        if change is not None:
            changed.append(change)

    diff = OntologyDiff(
        concepts_added=sorted(after_concepts - before_concepts),
        concepts_removed=sorted(before_concepts - after_concepts),
        instances_added=sorted(set(after_instances) - set(before_instances)),
        instances_removed=sorted(set(before_instances) - set(after_instances)),
        instances_changed=changed,
        coverage_before=before.get("coverage") or {},
        coverage_after=after.get("coverage") or {},
    )

    logger.info(f"Diff: {diff.summary()}")
    return diff
