"""Gap resolution and user assertions.

A checklist gap is either an extraction failure or a genuine silence in the
document, and the two need opposite responses:

    checklist item unsatisfied
            |
            +--> targeted retrieval for that specific item
            |
            +-- evidence found ---> EXTRACTION GAP
            |                       re-extract that concept. Do NOT ask the user.
            |
            +-- nothing found ----> DOCUMENT GAP
                                    ask the user; ratify the answer as an assertion

**The retrieval step is load-bearing.** Skip it and the system asks people to
compensate for its own defects: the gap disappears from the dashboard, the
extraction bug survives, and it recurs on every future document. Since extraction
samples by design (0.3% reach on a 12.9 MB spec), most gaps today are extraction
gaps, not document gaps.

User answers live in their own store rather than only inside the ontology.
Re-extraction rebuilds the ontology from the document and will never reproduce
them — they are not in the document — so they are carried forward deliberately
and revalidated each run.

See todo/07-gap-resolution-and-user-assertions.md.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from phases.phase1_models import (
    SOURCE_USER_ASSERTION,
    ConceptInstance,
    ConceptType,
    Ontology,
    slugify,
)

logger = logging.getLogger(__name__)

# How a gap was classified.
GAP_EXTRACTION = "extraction_gap"
GAP_DOCUMENT = "document_gap"
GAP_UNCHECKED = ""

# Checklist item states — the existing open/resolved/deferred model.
STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUS_DEFERRED = "deferred"

# What revalidating an assertion against a new extraction concluded.
OUTCOME_CARRIED = "carried"
OUTCOME_PROMOTED = "promoted"
OUTCOME_CONTRADICTED = "contradicted"
OUTCOME_UNMATCHED = "unmatched"

# Assertion lifecycle.
ASSERTION_ACTIVE = "active"
ASSERTION_REDUNDANT = "redundant"
ASSERTION_CONTRADICTED = "contradicted"

GAP_TOP_K = 6

# The retrieval width behind objective 7's central decision: evidence in the
# document means extraction missed it, no evidence means the document is silent
# and a person gets asked. A narrower probe turns the first into the second and
# sends a question to a human that the document already answers, so which width
# was in force is part of reading any gap verdict.
SETTINGS_PROCESS = "gap_resolution"
DEFAULT_SETTINGS = {"gap_top_k": GAP_TOP_K}


# ---------------------------------------------------------------------------
# Checklist
# ---------------------------------------------------------------------------


@dataclass
class ChecklistItem:
    """One thing the ontology is expected to answer and currently does not."""

    item_id: str
    question: str
    concept_type: str = ""
    status: str = STATUS_OPEN
    kind: str = GAP_UNCHECKED
    evidence_chunks: List[int] = field(default_factory=list)
    evidence_excerpt: str = ""
    resolution_notes: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def is_open(self) -> bool:
        return self.status == STATUS_OPEN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "question": self.question,
            "concept_type": self.concept_type,
            "status": self.status,
            "kind": self.kind,
            "evidence_chunks": self.evidence_chunks,
            "evidence_excerpt": self.evidence_excerpt,
            "resolution_notes": self.resolution_notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChecklistItem":
        return cls(
            item_id=data.get("item_id", ""),
            question=data.get("question", ""),
            concept_type=data.get("concept_type", ""),
            status=data.get("status", STATUS_OPEN),
            kind=data.get("kind", GAP_UNCHECKED),
            evidence_chunks=data.get("evidence_chunks", []),
            evidence_excerpt=data.get("evidence_excerpt", ""),
            resolution_notes=data.get("resolution_notes", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


def checklist_to_dict(items: List[ChecklistItem]) -> Dict[str, Any]:
    return {
        "items": [i.to_dict() for i in items],
        "open": sum(1 for i in items if i.status == STATUS_OPEN),
        "resolved": sum(1 for i in items if i.status == STATUS_RESOLVED),
        # A deferred item is a known unknown, which is the point of the state. It
        # is counted separately so it can never be totted up as resolved.
        "deferred": sum(1 for i in items if i.status == STATUS_DEFERRED),
        "updated_at": datetime.now().isoformat(),
    }


def checklist_from_dict(data: Dict[str, Any]) -> List[ChecklistItem]:
    return [ChecklistItem.from_dict(d) for d in (data or {}).get("items", [])]


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


@dataclass
class Assertion:
    """A fact the user supplied because the document does not state it.

    Traceable to a person and a time rather than to a passage. Assertions are
    axioms for anything downstream and must stay visibly labelled — silently
    promoting one to look document-sourced is the point where "traceable" would
    quietly become false.
    """

    concept_type: str
    name: str
    description: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)
    asserted_by: str = ""
    asserted_at: str = field(default_factory=lambda: datetime.now().isoformat())
    question: str = ""
    checklist_item: str = ""
    status: str = ASSERTION_ACTIVE
    notes: str = ""

    @property
    def instance_id(self) -> str:
        return f"{slugify(self.concept_type)}:{slugify(self.name)}"

    def to_instance(self) -> ConceptInstance:
        """Render as an ontology instance, labelled as a user assertion."""
        return ConceptInstance(
            name=self.name,
            description=self.description,
            attributes=dict(self.attributes),
            concept_type=self.concept_type,
            source=SOURCE_USER_ASSERTION,
            asserted_by=self.asserted_by,
            asserted_at=self.asserted_at,
            question=self.question,
            checklist_item=self.checklist_item,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.instance_id,
            "concept_type": self.concept_type,
            "name": self.name,
            "description": self.description,
            "attributes": self.attributes,
            "asserted_by": self.asserted_by,
            "asserted_at": self.asserted_at,
            "question": self.question,
            "checklist_item": self.checklist_item,
            "status": self.status,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Assertion":
        return cls(
            concept_type=data.get("concept_type", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            attributes=data.get("attributes", {}) or {},
            asserted_by=data.get("asserted_by", ""),
            asserted_at=data.get("asserted_at", ""),
            question=data.get("question", ""),
            checklist_item=data.get("checklist_item", ""),
            status=data.get("status", ASSERTION_ACTIVE),
            notes=data.get("notes", ""),
        )


# ---------------------------------------------------------------------------
# Step 1 — retrieve before asking
# ---------------------------------------------------------------------------


def _parse_verdict(response: str) -> Dict[str, Any]:
    """Read the model's judgement, defaulting to 'no evidence' when unreadable.

    Defaulting the other way would classify an unparseable answer as an
    extraction gap and quietly suppress a question that should have been asked.
    Neither default is free; this one fails toward asking a human rather than
    toward silence.
    """
    if not response:
        return {"answered": False, "evidence": "", "contradicts": False}

    fenced = re.search(r"```(?:json)?\s*(.+?)```", response, re.DOTALL)
    if fenced:
        response = fenced.group(1)

    match = re.search(r"\{.*\}", response, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return {
                "answered": bool(data.get("answered")),
                "evidence": str(data.get("evidence", "")).strip(),
                "contradicts": bool(data.get("contradicts")),
            }
        except json.JSONDecodeError:
            pass

    logger.warning(f"Unparseable gap verdict: {response[:120]!r}")
    return {"answered": False, "evidence": "", "contradicts": False}


def classify_gap(
    item: ChecklistItem,
    searcher,
    llm_client,
    top_k: Optional[int] = None,
    settings=None,
    db_session=None,
) -> ChecklistItem:
    """Retrieve for this specific item and decide which kind of gap it is.

    Evidence in the document means the extraction missed it — re-extract, do not
    ask. No evidence means the document is silent, which is the only case where
    a person should be asked.
    """
    from phases.settings_registry import settings_for

    if top_k is None:
        resolved = settings_for(SETTINGS_PROCESS, DEFAULT_SETTINGS, settings, db_session)
        top_k = resolved.get("gap_top_k", GAP_TOP_K)

    query = f"{item.concept_type} {item.question}".strip()
    context, indices = searcher.get_context_with_indices(query, top_k=top_k)

    prompt = f"""Decide whether the text below answers a specific question.

QUESTION: {item.question}

RETRIEVED TEXT:
{context}

Return ONLY a JSON object:
{{"answered": true/false,
  "evidence": "the sentence that answers it, verbatim, or empty",
  "contradicts": true/false}}

Set "answered" true only if the text genuinely states the answer. Do not infer
it, and do not answer from your own knowledge — the question is whether THIS
text says it. Set "contradicts" true if the text states something incompatible
with the question's premise."""

    verdict = _parse_verdict(llm_client.generate(prompt))

    item.evidence_chunks = indices
    if verdict["answered"]:
        item.kind = GAP_EXTRACTION
        item.evidence_excerpt = verdict["evidence"]
        logger.info(f"[Gap] {item.item_id}: extraction gap — evidence found, not asking the user")
    else:
        item.kind = GAP_DOCUMENT
        item.evidence_excerpt = ""
        logger.info(f"[Gap] {item.item_id}: document gap — nothing found, a person can be asked")

    item.updated_at = datetime.now().isoformat()
    return item


def classify_gaps(
    items: List[ChecklistItem], searcher, llm_client, settings=None, db_session=None
) -> List[ChecklistItem]:
    """Classify every open item. One failure must not lose the rest."""
    from phases.settings_registry import settings_for

    # Resolved once for the batch, so every item in one checklist was judged at
    # the same retrieval width.
    resolved = settings_for(SETTINGS_PROCESS, DEFAULT_SETTINGS, settings, db_session)

    for item in items:
        if not item.is_open:
            continue
        try:
            classify_gap(item, searcher, llm_client, settings=resolved)
        except Exception as e:
            logger.error(f"[Gap] Failed to classify {item.item_id}: {e}")
    return items


def questions_for_user(items: List[ChecklistItem]) -> List[ChecklistItem]:
    """Only document gaps may be put to a person.

    An unchecked item is deliberately excluded: asking before retrieving is the
    failure this module exists to prevent, so it cannot be reached by accident.
    """
    return [i for i in items if i.is_open and i.kind == GAP_DOCUMENT]


def phrase_question(item: ChecklistItem, ontology: Ontology) -> str:
    """Ask in a way that invites correction rather than invention.

    Showing what *was* found lets someone say "no, you missed it" — a bare
    question invites them to make something up.
    """
    concept = ontology.concept(item.concept_type) if item.concept_type else None
    known = [i.name for i in concept.instances[:5]] if concept else []

    if known:
        found = ", ".join(known)
        more = "" if concept and len(concept.instances) <= 5 else ", …"
        return (
            f"The document describes {item.concept_type}: {found}{more}. "
            f"It does not appear to state: {item.question} "
            f"Is that right, or has it been missed?"
        )
    return (
        f"Nothing in the document appears to state: {item.question} "
        f"Is that right, or has it been missed?"
    )


# ---------------------------------------------------------------------------
# Step 2 — ratify an answer
# ---------------------------------------------------------------------------


def record_answer(db_session, workflow_id: str, item: ChecklistItem, feedback: str,
                  phase_name: str = "phase1") -> None:
    """Put a person's answer into the audit trail beside the run that asked.

    `UserInteraction` and `RunTracker.record_interaction` have both existed since
    todo/10 and nothing ever called either, so answers lived only in
    `assertions.json` on disk. The consequence became concrete once the run
    report existed: it can say which runs need a human and never what a human
    decided, so a reviewer who resolves a gap leaves no mark on the record of the
    run that raised it.

    Never raises. An answer that is already stored must not be lost because the
    trail was unavailable — the assertion file remains the source of truth for
    the answer itself, and this is the record that it happened.
    """
    if db_session is None or not workflow_id:
        return
    try:
        from phases.run_tracker import RunTracker

        RunTracker(db_session, workflow_id, workflow_id,
                   phase_name=phase_name).record_interaction(item.question, feedback)
    except Exception as e:
        logger.warning(f"[Gap] Could not record the answer to {item.item_id}: {e}")


def ratify(
    item: ChecklistItem,
    name: str,
    asserted_by: str,
    description: str = "",
    attributes: Optional[Dict[str, Any]] = None,
    db_session=None,
    workflow_id: str = "",
) -> Assertion:
    """Turn a user's answer to a document gap into a stored assertion.

    Refuses on an extraction gap. That is the whole point of classifying first:
    if the document does state it, the fix is to extract it properly, and
    accepting a typed answer instead would bury the defect.
    """
    if item.kind == GAP_EXTRACTION:
        raise ValueError(
            f"{item.item_id} is an extraction gap — the document does state this "
            f"({item.evidence_excerpt[:80]!r}). Re-extract rather than asking a person."
        )
    if item.kind != GAP_DOCUMENT:
        raise ValueError(
            f"{item.item_id} has not been checked against the document yet; "
            f"call classify_gap() before asking anyone."
        )
    if not asserted_by:
        raise ValueError("An assertion must record who made it")

    assertion = Assertion(
        concept_type=item.concept_type,
        name=name,
        description=description,
        attributes=attributes or {},
        asserted_by=asserted_by,
        question=item.question,
        checklist_item=item.item_id,
    )

    item.status = STATUS_RESOLVED
    item.resolution_notes = f"Answered by {asserted_by} as a user assertion"
    item.updated_at = datetime.now().isoformat()

    logger.info(f"[Gap] {item.item_id} resolved by assertion {assertion.instance_id}")
    record_answer(
        db_session, workflow_id, item,
        f"Answered by {asserted_by}: {name}"
        + (f" — {description}" if description else "")
        + f" (assertion {assertion.instance_id})",
    )
    return assertion


def defer(item: ChecklistItem, notes: str = "", db_session=None,
          workflow_id: str = "") -> ChecklistItem:
    """Mark an item nobody could answer. It stays visible and never counts as resolved."""
    item.status = STATUS_DEFERRED
    item.resolution_notes = notes or "Deferred — no answer available"
    item.updated_at = datetime.now().isoformat()
    # A deferral is a decision a person made and is worth the same record as an
    # answer: "nobody could answer this" is a finding about the document.
    record_answer(db_session, workflow_id, item,
                  f"Deferred: {item.resolution_notes}")
    return item


# ---------------------------------------------------------------------------
# Step 3 — carry assertions across re-extraction
# ---------------------------------------------------------------------------


def apply_assertions(ontology: Ontology, assertions: List[Assertion]) -> Ontology:
    """Merge active assertions into a freshly extracted ontology.

    An assertion whose concept type no longer exists creates it, rather than
    being dropped — losing a human answer because a concept was renamed is
    exactly the silent loss the store exists to prevent.
    """
    for assertion in assertions:
        if assertion.status != ASSERTION_ACTIVE:
            continue

        concept = ontology.concept(assertion.concept_type)
        if concept is None:
            concept = ConceptType(
                name=assertion.concept_type,
                description=f"Introduced by a user assertion ({assertion.asserted_by})",
            )
            ontology.concept_types.append(concept)

        if concept.instance(assertion.instance_id) is not None:
            continue  # the document now covers it; revalidation handles promotion

        instance = assertion.to_instance()
        instance.concept_type = concept.name
        concept.instances.append(instance)

    return ontology


def revalidate_assertions(
    assertions: List[Assertion],
    ontology: Ontology,
    searcher,
    llm_client,
    settings=None,
    db_session=None,
) -> List[Dict[str, Any]]:
    """Re-check each assertion against a newly extracted ontology and document.

    Every assertion resolves to exactly one outcome:

      * **carried** — still absent from the document; remains a user assertion
      * **promoted** — the document now covers it; the assertion becomes
        redundant but is kept for history
      * **contradicted** — the document now says something incompatible; flagged,
        never silently dropped and never silently kept

    The third case is the one that matters over time. Without it assertions
    accumulate and drift out of agreement with the source, and nothing notices.
    """
    from phases.settings_registry import settings_for

    resolved = settings_for(SETTINGS_PROCESS, DEFAULT_SETTINGS, settings, db_session)
    top_k = resolved.get("gap_top_k", GAP_TOP_K)

    outcomes = []

    for assertion in assertions:
        if assertion.status == ASSERTION_CONTRADICTED:
            # Already flagged and awaiting a human; re-judging it each run would
            # let a later pass quietly overturn a decision nobody has made yet.
            outcomes.append(_outcome(assertion, OUTCOME_CONTRADICTED, "Awaiting review"))
            continue

        probe = f"{assertion.concept_type} {assertion.name} {assertion.description}".strip()
        context, indices = searcher.get_context_with_indices(probe, top_k=top_k)

        prompt = f"""A person previously stated the following, because the document did not say it.

STATEMENT: {assertion.concept_type}: {assertion.name}. {assertion.description}

The document has since been re-read. Here is the most relevant text:

{context}

Return ONLY a JSON object:
{{"answered": true/false,
  "evidence": "the sentence that states it, verbatim, or empty",
  "contradicts": true/false}}

"answered" true means the text now states this. "contradicts" true means the
text states something incompatible with it. Judge only from the text above."""

        verdict = _parse_verdict(llm_client.generate(prompt))

        if verdict["contradicts"]:
            assertion.status = ASSERTION_CONTRADICTED
            assertion.notes = verdict["evidence"]
            outcomes.append(
                _outcome(assertion, OUTCOME_CONTRADICTED, verdict["evidence"], indices)
            )
            logger.warning(
                f"[Gap] Assertion {assertion.instance_id} is contradicted by the document"
            )
            continue

        if verdict["answered"]:
            assertion.status = ASSERTION_REDUNDANT
            assertion.notes = verdict["evidence"]
            # The instance stays in the ontology but is now document-sourced; the
            # assertion is kept in the store as history rather than deleted.
            _promote(ontology, assertion, indices)
            outcomes.append(_outcome(assertion, OUTCOME_PROMOTED, verdict["evidence"], indices))
            logger.info(f"[Gap] Assertion {assertion.instance_id} is now covered by the document")
            continue

        outcomes.append(_outcome(assertion, OUTCOME_CARRIED, ""))

    return outcomes


def _promote(ontology: Ontology, assertion: Assertion, indices: List[int]) -> None:
    """Relabel an instance the document now covers as document-sourced.

    Only ever in this direction, and only after the document has been re-read and
    found to state it. The reverse — quietly making a user answer look like it
    came from the source — is never done.
    """
    from phases.phase1_models import SOURCE_DOCUMENT

    instance = ontology.find_instance(assertion.instance_id)
    if instance is None:
        return
    instance.source = SOURCE_DOCUMENT
    instance.source_chunk = indices[0] if indices else None
    instance.source_document = ""
    instance.asserted_by = ""
    instance.asserted_at = ""


def _outcome(
    assertion: Assertion, outcome: str, evidence: str, chunks: Optional[List[int]] = None
) -> Dict[str, Any]:
    return {
        "id": assertion.instance_id,
        "name": assertion.name,
        "concept_type": assertion.concept_type,
        "outcome": outcome,
        "evidence": evidence,
        "chunks": chunks or [],
        "status": assertion.status,
    }


def match_assertions_to_ontology(
    assertions: List[Assertion], ontology: Ontology
) -> Tuple[List[Assertion], List[Assertion]]:
    """Split assertions into those whose concept type still exists and those adrift.

    A renamed concept surfaces here as "assertion no longer matches an instance",
    which is a review item rather than silent loss.
    """
    matched, unmatched = [], []
    for assertion in assertions:
        if ontology.concept(assertion.concept_type) is not None:
            matched.append(assertion)
        else:
            unmatched.append(assertion)
    return matched, unmatched


# ---------------------------------------------------------------------------
# Assumptions become questions
# ---------------------------------------------------------------------------


def checklist_from_assumptions(
    requirements, existing: Optional[List[ChecklistItem]] = None,
) -> List[ChecklistItem]:
    """Turn Phase 2's assumptions into items a person can be asked about.

    An assumption is a requirement asserting something the document does not
    state — `limit=0`, `limit=-1`, an undocumented 401. As test design that is
    often exactly right, which is why they are labelled rather than deleted. But
    a label is not a question, and until now nothing carried them any further:
    they shipped marked as assumptions and no one was ever asked to confirm one.

    This is the same shape as a **document gap** — something the document is
    silent on — which is precisely the case the checklist flow exists to route
    to a human. So they enter it already classified as `GAP_DOCUMENT`: they have
    been checked against the document by the entailment judge, which is a
    stronger check than `classify_gap` performs, and re-classifying them would
    pay for that twice.

    Existing items are matched by `item_id` and left alone, so re-running Phase 2
    does not reopen a question someone has already answered or deferred.
    """
    by_id = {item.item_id: item for item in (existing or [])}
    out: List[ChecklistItem] = list(existing or [])

    for requirement in requirements:
        if not getattr(requirement, "is_assumption", False):
            continue

        item_id = f"assumption:{requirement.id}"
        if item_id in by_id:
            continue

        basis = getattr(requirement, "assumption_basis", "") or ""
        out.append(ChecklistItem(
            item_id=item_id,
            question=(
                f"{requirement.title} — the document does not state this. "
                f"Is it true of the system?"
            ),
            concept_type=getattr(requirement, "category", "") or "requirement",
            # Already checked against the document, and by a stronger check than
            # classify_gap: the entailment judge read the cited passages and
            # found they do not establish the claim.
            kind=GAP_DOCUMENT,
            evidence_excerpt=basis[:400],
        ))

    return out
