"""Ask an LLM, once per document, what a "well-formed claim" should look like
here — then clamp the answer down to only what's actually safe to apply.

### Why this exists

`phases/requirement_shapes.py`'s shape check is domain-agnostic by default
(see `claimvalidator/claim_shims.py::BARE_CLAIM_RULES`): it only asks whether
a claim has enough text to be worth judging. The engine underneath supports a
richer, domain-shaped rule too — `require_subject` + `subject_pattern`, e.g.
"a claim's subject should look like `GET /orders/{id}`, not just a bare topic
like 'orders'" — but nothing populates it today. This module lets an LLM
propose that richer rule once, from the ontology's own already-discovered
concept types, rather than a human hand-authoring a profile per domain.

### Why the answer gets clamped, not trusted verbatim

The object the shape check actually sees for a bare `id + text` claim
(`claim_shims.py::_ShapeClaim`) exposes almost no domain-specific surface: its
`.criteria` is always `[]`, `.title` is always non-empty, and it never sets
`.endpoint` at all. Against that shim:

- `require_subject: True` doesn't selectively enforce anything — it flags
  every single claim, unconditionally, because there's no subject to check.
- `id_pattern` would match a caller-chosen label ("C1", "req-42"), not
  anything the document determines — there's no safe way to apply it here.
- `require_any_of: ["criteria"]` alone would fail every claim, since
  `.criteria` is never populated.

A model asked to propose a shape profile has no way to know any of this — it
only sees the document. So this module treats its proposal as a set of
*intentions*, not instructions: keep what's safe, force what must be forced,
drop what can never work, and write down what changed and why. Every claim
this produces is inert or a strict narrowing of today's default — it can
never make the shape check *more* aggressive than the modes already proven
safe against this shim.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

MAX_PATTERN_CHARS = 500
MAX_FIELD_CHARS = 100
MAX_LIST_ENTRIES = 20
MAX_DESCRIPTION_CHARS = 500

_SAFE_REQUIRE_FIELDS = {"title", "id"}
_SAFE_REQUIRE_ANY_OF = {"expected_behavior", "criteria"}


class ShapeProfileInferenceError(Exception):
    """The model's response had nothing usable in it at all — caller should
    keep the static default rather than store this result."""


@dataclass
class InferredProfile:
    """A shape-rule proposal, already clamped to what's safe to apply.

    `rules` is in the exact shape `phases/requirement_shapes.py` expects:
    `{"requirement": {...}}`. `notes` records every place the model's raw
    proposal was changed or dropped, so a person reading the ontology's
    metadata later can see what happened, not just the final result.
    """

    rules: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


def _load_json(response: str) -> Any:
    """Pull the first JSON object out of a model response.

    Duplicated from `phases/phase1_generic_extractor.py::_load_json` rather
    than imported — that helper isn't public API across modules, and it's
    small and stable enough that a second copy is cheaper than a new shared
    module for one caller.
    """
    if not response:
        return None

    fenced = re.search(r"```(?:json)?\s*(.+?)```", response, re.DOTALL)
    if fenced:
        response = fenced.group(1)

    match = re.search(r"\{.*\}", response, re.DOTALL)
    if not match:
        logger.warning(f"[ShapeProfile] No parseable JSON in response: {response[:120]!r}")
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        logger.warning(f"[ShapeProfile] Unparseable JSON in response: {response[:120]!r}")
        return None


def _clamp_list(raw: Any, allowed: set, notes: List[str], field_name: str) -> List[str]:
    if not isinstance(raw, list):
        raw = [raw] if raw else []
    kept = []
    dropped = []
    for item in raw[:MAX_LIST_ENTRIES]:
        item = str(item).strip()[:MAX_FIELD_CHARS]
        if item in allowed:
            kept.append(item)
        elif item:
            dropped.append(item)
    if dropped:
        notes.append(f"{field_name}: dropped {dropped!r} — not among {sorted(allowed)}")
    return kept


def _clamp_pattern(raw: Any, field_name: str, notes: List[str]) -> str:
    """Regex-validate and length-cap a proposed pattern. Returns "" (never
    applied) on anything that doesn't compile — a broken pattern must never
    reach `meta.json`, since it would otherwise sit there ready to raise the
    moment something turns it on."""
    if not raw or not isinstance(raw, str):
        return ""
    pattern = raw.strip()[:MAX_PATTERN_CHARS]
    try:
        re.compile(pattern)
    except re.error as e:
        notes.append(f"{field_name}: model proposed {pattern!r}, not a valid regex ({e}) — dropped")
        return ""
    return pattern


def infer_shape_profile(
    concept_types: List[Dict[str, Any]],
    background_description: str,
    llm_client,
) -> InferredProfile:
    """One call, made once per document — not once per claim.

    Raises `ShapeProfileInferenceError` only when the model's response had
    nothing usable in it at all (unparseable, or an object with no relevant
    keys). Any partially-usable response still returns an `InferredProfile`,
    clamped down to whatever part of it was safe to keep.
    """
    concept_summary = "\n".join(
        f"- {c.get('name', '?')}: {c.get('description', '')}"
        for c in (concept_types or [])[:30]
    ) or "(no concept types discovered)"

    prompt = f"""A document has been read and these concept types were found in it:

{concept_summary}

What this document is: {background_description or "(not specified)"}

A separate system checks whether a submitted claim about this document has
enough structure to be worth judging at all — before checking whether the
claim is actually TRUE, it first checks the claim's SHAPE. Propose shape
rules suited to this specific document. You may propose:

- "require_fields": which of ["title", "id"] a claim must have (usually
  both are already guaranteed present — only include one if you have a
  real reason).
- "require_any_of": at least one of ["expected_behavior", "criteria"] must
  be present.
- "wants_subject_check": true/false — whether a claim about this document
  should name a specific, concrete thing (e.g. an API operation like
  "GET /orders/{{id}}") rather than just a general topic. NOTE: this is
  recorded for information only in the current deployment and is not yet
  enforced — still answer honestly, it helps future versions of this check.
- "subject_pattern": if wants_subject_check is true, a regular expression
  a well-formed subject should match.
- "id_pattern": a regular expression claim IDs in this domain typically
  follow, if the document suggests a real convention (e.g. "REQ-\\\\d+").
- "description": one sentence describing what makes a claim well-formed
  for this specific document.

Respond with ONLY a JSON object with whichever of these keys you have a
real opinion on. Omit any key you have nothing useful to say about."""

    response = llm_client.generate(prompt)
    data = _load_json(response)

    if not isinstance(data, dict) or not data:
        raise ShapeProfileInferenceError("model response contained no usable JSON object")

    notes: List[str] = []

    require_fields = _clamp_list(data.get("require_fields"), _SAFE_REQUIRE_FIELDS, notes, "require_fields")
    require_any_of = _clamp_list(data.get("require_any_of"), _SAFE_REQUIRE_ANY_OF, notes, "require_any_of")
    if "expected_behavior" not in require_any_of:
        require_any_of.append("expected_behavior")
        notes.append(
            "require_any_of: 'expected_behavior' added — a claim's own text is the only "
            "thing this pipeline ever actually populates; 'criteria' alone is never "
            "satisfiable for a bare claim and would fail every one"
        )

    if data.get("require_subject") is True:
        notes.append(
            "require_subject: model requested True — not applied. Bare claims never carry "
            "a 'subject'/'endpoint' field in this pipeline today, so turning this on would "
            "flag every claim unconditionally, not selectively (see this module's docstring)"
        )

    if data.get("id_pattern"):
        notes.append(
            f"id_pattern: model proposed {str(data.get('id_pattern'))[:MAX_FIELD_CHARS]!r} — "
            f"dropped unconditionally. A claim's id is caller-chosen (e.g. 'C1'), not "
            f"determined by the document, so no document-derived pattern can validly match it"
        )

    subject_pattern = ""
    if data.get("wants_subject_check") is True:
        subject_pattern = _clamp_pattern(data.get("subject_pattern"), "subject_pattern", notes)

    description = str(data.get("description") or "").strip()[:MAX_DESCRIPTION_CHARS]

    rule: Dict[str, Any] = {
        "description": description or "Shape profile inferred for this document.",
        "require_fields": require_fields,
        "require_any_of": require_any_of,
        # Always forced off — see module docstring. Kept as an explicit key
        # (not omitted) so a reader of meta.json sees the decision, not an
        # absence that looks like an oversight.
        "require_subject": False,
    }
    if subject_pattern:
        # Inert while require_subject is False (the check short-circuits
        # before ever reading it) — stored anyway as a forward-looking,
        # informational record of what the model thought this document's
        # subjects should look like.
        rule["subject_pattern"] = subject_pattern

    return InferredProfile(rules={"requirement": rule}, notes=notes)
