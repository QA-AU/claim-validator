"""Is each instance really an instance of the concept it was filed under?

Every other signal in this pipeline answers a different question. Structure asks
"is this well-formed", coverage asks "how much was read", citation rate asks "can
this be traced". A tag filed as an endpoint scores perfectly on all three — it is
well-formed, it was read, and it cites a real passage. It is simply the wrong
kind of thing.

Two checks, cheapest first:

1. **Shape rules** — deterministic, free, no model. A profile declares what an
   instance of a bucket must look like; an instance that does not is reported.
2. **The judge** — one model call. Shows each instance *beside the passage it
   cited* and asks whether that text supports the typing. Only possible because
   per-instance provenance exists: before it, there was no specific text to
   check against.

Neither ever deletes anything. Both report, because a false positive that
silently removed a real instance would be worse than the error being caught.

### Why shape rules check attributes, not names

Measured on the live GitHub run (2026-08-14). Real endpoints were named in prose
— "Delete a file", "Create or update file contents" — and carried the actual
operation in their attributes (`http_method: DELETE`, `path: /repos/...`). The
mislabelled tag `repos` carried **no attributes at all**.

So a name-based rule would have flagged 4 genuine endpoints out of 4 and missed
the one real error. Attributes separate them cleanly. Names are still accepted as
an alternative, because some models do put "GET /orders" in the name.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from phases.phase1_models import SOURCE_DOCUMENT, Ontology

logger = logging.getLogger(__name__)

# How many instances the judge looks at in one call. The point is a spot check
# that makes a systematic mislabelling visible, not an audit of every row.
JUDGE_SAMPLE = 15

# A cost/coverage knob rather than a correctness one: a larger sample catches a
# mislabelling in a smaller concept, and costs proportionally. Held in the
# database so the size a run actually used is recorded with it.
SETTINGS_PROCESS = "type_check"
DEFAULT_SETTINGS = {
    "judge_sample": JUDGE_SAMPLE,
    # Whether Phase 1 asks a model to check that each instance really is the
    # kind of thing it was filed as. Free checks cannot catch this: a tag filed
    # as an endpoint has a name, a citation and no duplicate, so every
    # deterministic check passes it. That failure took 19 of 21 extracted
    # endpoints on the GitHub specification.
    "run_judge": True,
    # Repeats, for the same reason the entailment judge repeats.
    "judge_runs": 3,
}

# The cited passage is shown in full. An earlier value of 700 truncated the
# 1000-character chunks and produced exactly the failure you would expect:
# "Pembrolizumab 200 mg" sits at character 754 of its chunk, so the judge was
# shown 70% of a passage, told it was the passage, and then reported that the
# evidence was not in it. It was right; the truncation lied to it.
#
# Tied to the chunk size rather than a literal, so a re-chunked index cannot
# silently reintroduce the same blind spot. The headroom covers the marker text.
from phases.phase1_rag_indexer import CHUNK_SIZE as _CHUNK_SIZE

JUDGE_CHUNK_CHARS = _CHUNK_SIZE + 200


@dataclass
class ShapeViolation:
    """An instance that does not look like the kind of thing it is filed under."""

    instance_id: str
    concept: str
    bucket: str
    name: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.instance_id,
            "concept": self.concept,
            "bucket": self.bucket,
            "name": self.name,
            "reason": self.reason,
        }


# Verdicts. Kept apart because they mean different things and only one of them
# is this module's job.
VERDICT_OK = "ok"
VERDICT_WRONG_KIND = "wrong_kind"  # a tag filed as an endpoint — what we are hunting
VERDICT_WEAK_CITATION = "weak_citation"  # right kind, but the passage does not show it

# Ascending order of accusation, for breaking a tie no majority resolved. The
# same rule the entailment judge uses: a judge that cannot agree with itself
# must not be the reason an instance is called mis-typed.
_ACCUSATION_ORDER = (VERDICT_OK, VERDICT_WEAK_CITATION, VERDICT_WRONG_KIND)



@dataclass
class TypeVerdict:
    """The judge's opinion on one instance, checked against its cited passage."""

    instance_id: str
    concept: str
    name: str
    verdict: str = VERDICT_OK
    reason: str = ""
    # How many runs backed this verdict, out of how many judged the instance.
    # A `wrong_kind` verdict accuses, and the entailment judge measured
    # single-run verdicts moving on 23% of items — so this one repeats too.
    agreement: int = 1
    runs_judged: int = 1

    @property
    def supported(self) -> bool:
        """Correctly typed. A weak citation is still the right *kind* of thing."""
        return self.verdict != VERDICT_WRONG_KIND

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.instance_id,
            "concept": self.concept,
            "name": self.name,
            "verdict": self.verdict,
            "supported": self.supported,
            "reason": self.reason,
        }


@dataclass
class TypeCheckReport:
    shape_violations: List[ShapeViolation] = field(default_factory=list)
    verdicts: List[TypeVerdict] = field(default_factory=list)
    judged: int = 0
    skipped_uncited: int = 0

    @property
    def unsupported(self) -> List[TypeVerdict]:
        """Mis-typed instances only. Weak citations are reported separately.

        Measured on the reconstructed GitHub failure: of 7 non-`ok` verdicts,
        4 were genuine mis-typings and 3 were "the passage does not show this".
        Folding the latter into the type flag would put a ~7% noise rate on a
        correct ontology, which is how a flag gets ignored.
        """
        return [v for v in self.verdicts if v.verdict == VERDICT_WRONG_KIND]

    @property
    def weakly_cited(self) -> List[TypeVerdict]:
        return [v for v in self.verdicts if v.verdict == VERDICT_WEAK_CITATION]

    def review_flags(self) -> List[str]:
        flags = []

        if self.shape_violations:
            by_bucket: Dict[str, List[ShapeViolation]] = {}
            for violation in self.shape_violations:
                by_bucket.setdefault(violation.bucket, []).append(violation)
            for bucket, violations in sorted(by_bucket.items()):
                names = ", ".join(f"{v.name!r}" for v in violations[:4])
                more = "" if len(violations) <= 4 else f" and {len(violations) - 4} more"
                flags.append(
                    f"{len(violations)} instance(s) filed as {bucket} do not look like one: "
                    f"{names}{more} — {violations[0].reason}"
                )

        if self.unsupported:
            names = ", ".join(f"{v.name!r}" for v in self.unsupported[:4])
            more = "" if len(self.unsupported) <= 4 else f" and {len(self.unsupported) - 4} more"
            flags.append(
                f"{len(self.unsupported)} of {self.judged} instances checked are the wrong "
                f"kind of thing for the concept they are filed under: {names}{more}"
            )

        if self.weakly_cited:
            names = ", ".join(f"{v.name!r}" for v in self.weakly_cited[:4])
            more = "" if len(self.weakly_cited) <= 4 else f" and {len(self.weakly_cited) - 4} more"
            flags.append(
                f"{len(self.weakly_cited)} instance(s) are correctly typed but their cited "
                f"passage does not clearly show them: {names}{more}"
            )

        return flags

    def sampling_note(self, total_instances: int) -> str:
        """Say plainly that this was a sample, and how large a one."""
        if not self.judged:
            return "No instances were judged."
        if self.judged >= total_instances:
            return f"All {total_instances} instances were judged."
        return (
            f"{self.judged} of {total_instances} instances were judged — a spot check. "
            f"An unjudged instance is not a cleared one."
        )

    @property
    def needs_review(self) -> bool:
        return bool(self.review_flags())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "shape_violations": [v.to_dict() for v in self.shape_violations],
            "verdicts": [v.to_dict() for v in self.verdicts],
            "judged": self.judged,
            "skipped_uncited": self.skipped_uncited,
            "unsupported_count": len(self.unsupported),
            "weak_citation_count": len(self.weakly_cited),
            "review_flags": self.review_flags(),
            "needs_review": self.needs_review,
        }


# ---------------------------------------------------------------------------
# 1. Shape rules — deterministic, no model
# ---------------------------------------------------------------------------


def _bucket_for(concept_name: str, profile) -> Optional[str]:
    """Which of the profile's buckets this concept maps to, if any.

    Matched the same loose way `phase2_adapter` matches, so a rule written for
    `endpoints` applies to whatever the model happened to call the concept.
    """
    normalised = concept_name.strip().lower().replace(" ", "_").replace("-", "_")
    parts = [w for w in normalised.split("_") if w]
    head = parts[-1] if parts else normalised

    # Whole words, not substrings. Loose substring matching sent `endpoint_tag`
    # to the `endpoints` bucket, so a concept the model had correctly separated
    # out — tags, kept apart from the operations they group — was judged against
    # the rule for operations and reported as a malformed endpoint. The concept
    # was right and the matcher was wrong.
    #
    # An exact bucket-name match still wins, so a concept genuinely called
    # `endpoints` maps whatever its wording.
    for bucket, aliases in (profile.buckets or {}).items():
        if normalised == bucket.lower() or normalised.rstrip("s") == bucket.lower().rstrip("s"):
            return bucket
        for alias in aliases:
            needle = alias.strip().lower().replace(" ", "_").replace("-", "_")
            if not needle:
                continue
            # A single-word alias must appear as a whole word; a multi-word one
            # must appear as a contiguous phrase.
            if "_" in needle:
                if needle in normalised:
                    return bucket
            elif head == needle or head.rstrip("s") == needle.rstrip("s"):
                # Head-final: in an English compound the last word says what the
                # thing *is*. `api_endpoint` is an endpoint; `endpoint_tag` is a
                # tag that happens to group endpoints, and judging it by the
                # endpoint rule reported a correctly separated concept as
                # malformed.
                return bucket
    return None


def _attribute_value(instance, key: str) -> Any:
    """Read an attribute, tolerating the flattened serialised form."""
    if hasattr(instance, "attributes"):
        return instance.attributes.get(key)
    return instance.get(key)


def check_shapes(ontology: Ontology, profile) -> List[ShapeViolation]:
    """Check every instance against its bucket's declared shape.

    Free and deterministic. Runs on every extraction; the judge does not have to.
    """
    rules = getattr(profile, "shape_rules", None) or {}
    if not rules:
        return []

    violations: List[ShapeViolation] = []

    for concept in ontology.concept_types:
        bucket = _bucket_for(concept.name, profile)
        rule = rules.get(bucket) if bucket else None
        if not rule:
            continue

        required = rule.get("satisfied_by_attributes") or []
        name_pattern = rule.get("satisfied_by_name_pattern")
        attribute_patterns = rule.get("attribute_patterns") or {}
        description = rule.get("description", f"expected an instance of {bucket}")

        for instance in concept.instances:
            # A user assertion describes what the document does not say, so it
            # has no obligation to look like an extracted instance.
            if instance.source != SOURCE_DOCUMENT:
                continue

            has_attribute = any(
                str(_attribute_value(instance, key) or "").strip() for key in required
            )
            name_matches = bool(
                name_pattern and re.search(name_pattern, instance.name or "")
            )

            if required or name_pattern:
                if not (has_attribute or name_matches):
                    violations.append(
                        ShapeViolation(
                            instance_id=instance.instance_id,
                            concept=concept.name,
                            bucket=bucket,
                            name=instance.name,
                            reason=description,
                        )
                    )
                    continue

            # Attributes whose absence is worth reporting even when the instance
            # is otherwise well-shaped.
            #
            # Found live: a run extracted 10 "endpoints" whose paths came from a
            # schema *example* — `/repos/octocat/Hello-World/commits{/sha}` —
            # with no HTTP method on any of them. Having a path satisfied the
            # rule, so nothing objected. A callable operation needs a method;
            # a URL on its own is just a string that looks like one.
            # The listed keys are *alternatives* — an endpoint carrying either
            # `http_method` or `method` is fine. Requiring all of them flagged
            # every genuine endpoint, since real data uses one name or the other.
            wanted = rule.get("warn_if_missing") or []
            if wanted:
                has_one = any(
                    str(_attribute_value(instance, key) or "").strip() for key in wanted
                )
                carried_by_name = bool(
                    name_pattern and re.search(name_pattern, instance.name or "")
                )
                if not has_one and not carried_by_name:
                    violations.append(
                        ShapeViolation(
                            instance_id=instance.instance_id,
                            concept=concept.name,
                            bucket=bucket,
                            name=instance.name,
                            reason=rule.get(
                                "warn_if_missing_reason", f"has none of: {', '.join(wanted)}"
                            ),
                        )
                    )
                    continue

            # A present attribute must still be well-formed. A path of "repos"
            # is as wrong as no path at all.
            for key, pattern in attribute_patterns.items():
                value = _attribute_value(instance, key)
                if value is None or not str(value).strip():
                    continue
                if not re.search(pattern, str(value)):
                    violations.append(
                        ShapeViolation(
                            instance_id=instance.instance_id,
                            concept=concept.name,
                            bucket=bucket,
                            name=instance.name,
                            reason=f"{key}={value!r} is not a valid {key}",
                        )
                    )

    if violations:
        logger.warning(f"[TypeCheck] {len(violations)} shape violation(s)")
    return violations


# ---------------------------------------------------------------------------
# 2. The judge — one model call, against the cited passage
# ---------------------------------------------------------------------------


def _parse_verdicts(response: str) -> List[Dict[str, Any]]:
    if not response:
        return []
    fenced = re.search(r"```(?:json)?\s*(.+?)```", response, re.DOTALL)
    if fenced:
        response = fenced.group(1)
    match = re.search(r"\[.*\]", response, re.DOTALL)
    if not match:
        logger.warning(f"[TypeCheck] Unparseable judge response: {response[:120]!r}")
        return []
    try:
        data = json.loads(match.group())
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        logger.warning(f"[TypeCheck] Judge returned malformed JSON: {response[:120]!r}")
        return []


def judge_types(
    ontology: Ontology,
    chunks: List[str],
    llm_client,
    sample_size: int = JUDGE_SAMPLE,
) -> TypeCheckReport:
    """Ask whether each sampled instance is supported by the passage it cited.

    Only instances with a verified citation can be judged — there is otherwise
    no specific text to judge against, and asking the model to assess a claim
    without evidence would just re-run the extraction.

    Sampled across concepts rather than taken in order, so a mislabelling
    confined to one concept cannot hide behind a long first concept.
    """
    report = TypeCheckReport()

    candidates = []
    for concept in ontology.concept_types:
        cited = [
            i
            for i in concept.instances
            if i.source == SOURCE_DOCUMENT
            and i.source_chunk is not None
            and 0 <= i.source_chunk < len(chunks)
        ]
        report.skipped_uncited += len(concept.instances) - len(cited)
        candidates.append((concept, cited))

    # Round-robin across concepts, but *strided* within each one rather than
    # taking the first N.
    #
    # Found by testing: mis-typed instances cluster and are usually appended
    # last (all four mislabelled tags sat at positions 5-8 of one concept). A
    # depth-first round robin took the first two of every concept and missed
    # every single one, reporting a clean bill of health on an ontology that had
    # exactly the defect this module exists to catch.
    per_concept = max(1, sample_size // max(1, len([c for c, cited in candidates if cited])))
    selected = []
    for concept, cited in candidates:
        if not cited:
            continue
        if len(cited) <= per_concept:
            picks = cited
        else:
            stride = len(cited) / per_concept
            picks = [cited[min(len(cited) - 1, int(i * stride))] for i in range(per_concept)]
            # The last instance is where appended mistakes land, so always
            # include it rather than letting the stride stop short.
            if cited[-1] not in picks:
                picks[-1] = cited[-1]
        selected.extend((concept, instance) for instance in picks)

    # Top up from anything not yet chosen, so a small ontology is fully covered.
    if len(selected) < sample_size:
        chosen = {id(i) for _, i in selected}
        for concept, cited in candidates:
            for instance in cited:
                if len(selected) >= sample_size:
                    break
                if id(instance) not in chosen:
                    selected.append((concept, instance))
                    chosen.add(id(instance))

    selected = selected[:sample_size]

    if not selected:
        logger.info("[TypeCheck] Nothing citable to judge")
        return report

    lines = []
    for concept, instance in selected:
        attributes = {k: v for k, v in instance.attributes.items() if v not in (None, "")}
        lines.append(
            f'- id: "{instance.instance_id}"\n'
            f"  filed_as: {concept.name} ({concept.description or 'no description'})\n"
            f"  name: {instance.name!r}\n"
            f"  attributes: {json.dumps(attributes)}\n"
            f"  cited_passage: {chunks[instance.source_chunk][:JUDGE_CHUNK_CHARS]!r}"
        )

    prompt = f"""Check whether each item below is really an instance of the concept it was
filed under, judging ONLY from the passage it cites.

{chr(10).join(lines)}

For each item return an object:
- id: the id exactly as given
- verdict: exactly one of
    "wrong_kind"     — the passage shows this is a DIFFERENT KIND of thing from
                       the concept it is filed under (for example a category, a
                       tag, a section heading, or a field name filed as an
                       operation)
    "weak_citation"  — it is plausibly the right kind of thing, but the cited
                       passage does not actually show it
    "ok"             — the passage supports it
- reason: a short phrase, only when the verdict is not "ok"

Judge the *kind* of thing. "wrong_kind" is reserved for a clear category error
— it drives a review, so a wrong one costs someone's attention. If you are
unsure whether it is mis-typed or merely poorly evidenced, use "weak_citation".
If the passage neither supports nor contradicts the typing, use "ok".

Return ONLY a JSON array."""

    verdicts_by_id = {}
    for item in _parse_verdicts(llm_client.generate(prompt)):
        if isinstance(item, dict) and item.get("id"):
            verdicts_by_id[str(item["id"])] = item

    for concept, instance in selected:
        raw = verdicts_by_id.get(instance.instance_id)
        if raw is None:
            # Unjudged is not the same as wrong. Absence of a verdict must not
            # become an accusation.
            continue

        verdict = str(raw.get("verdict", "")).strip().lower()
        if verdict not in (VERDICT_OK, VERDICT_WRONG_KIND, VERDICT_WEAK_CITATION):
            # Tolerate the older boolean shape, and default anything
            # unrecognised to ok — an unreadable verdict must not accuse.
            verdict = VERDICT_OK if raw.get("supported", True) else VERDICT_WRONG_KIND

        report.verdicts.append(
            TypeVerdict(
                instance_id=instance.instance_id,
                concept=concept.name,
                name=instance.name,
                verdict=verdict,
                reason=str(raw.get("reason", "")).strip(),
            )
        )

    report.judged = len(report.verdicts)
    if report.unsupported:
        logger.warning(
            f"[TypeCheck] {len(report.unsupported)} of {report.judged} instances "
            f"are not supported by their cited passage"
        )
    return report


def judge_types_repeated(
    ontology: Ontology,
    chunks: List[str],
    llm_client,
    sample_size: int = JUDGE_SAMPLE,
    runs: int = 3,
) -> TypeCheckReport:
    """Judge the sample several times and report the majority verdict.

    One call covers the whole sample, so three runs cost three calls — trivial
    beside extraction, and the reason to pay it is measured: repeating the
    entailment judge moved its verdict on 23% of items, and two separate
    single-run contradiction lists failed to reproduce at all. `wrong_kind`
    accuses an instance and sends someone to look at it, so it earns the same
    treatment.

    A tie takes the least accusatory verdict on the table, and the agreement is
    recorded on each verdict so `2/3` reads differently from `3/3`.
    """
    runs = max(1, int(runs))
    if runs == 1:
        return judge_types(ontology, chunks, llm_client, sample_size=sample_size)

    passes = [judge_types(ontology, chunks, llm_client, sample_size=sample_size)
              for _ in range(runs)]

    merged = TypeCheckReport()
    merged.skipped_uncited = passes[0].skipped_uncited
    merged.judged = max(p.judged for p in passes)

    seen: Dict[str, List[TypeVerdict]] = {}
    order: List[str] = []
    for report in passes:
        for verdict in report.verdicts:
            if verdict.instance_id not in seen:
                order.append(verdict.instance_id)
                seen[verdict.instance_id] = []
            seen[verdict.instance_id].append(verdict)

    for instance_id in order:
        votes = seen[instance_id]
        counts: Dict[str, int] = {}
        for vote in votes:
            counts[vote.verdict] = counts.get(vote.verdict, 0) + 1

        top = max(counts.values())
        tied = [v for v, n in counts.items() if n == top]
        winner = min(tied, key=lambda v: _ACCUSATION_ORDER.index(v)
                     if v in _ACCUSATION_ORDER else len(_ACCUSATION_ORDER))
        # The reason comes from a run that reached the reported verdict, so the
        # quoted words and the label cannot disagree.
        spoke = next(v for v in votes if v.verdict == winner)

        merged.verdicts.append(TypeVerdict(
            instance_id=instance_id,
            concept=spoke.concept,
            name=spoke.name,
            verdict=winner,
            reason=spoke.reason,
            agreement=counts[winner],
            runs_judged=len(votes),
        ))

    return merged


def full_check(
    ontology: Ontology,
    profile,
    chunks: Optional[List[str]] = None,
    llm_client=None,
    sample_size: Optional[int] = None,
    settings=None,
    db_session=None,
) -> TypeCheckReport:
    """Shape rules always; the judge only when there is a client and an index."""
    from phases.settings_registry import settings_for

    if sample_size is None:
        resolved = settings_for(SETTINGS_PROCESS, DEFAULT_SETTINGS, settings, db_session)
        sample_size = resolved.get("judge_sample", JUDGE_SAMPLE)

    report = TypeCheckReport()
    report.shape_violations = check_shapes(ontology, profile)

    if llm_client is not None and chunks:
        judged = judge_types(ontology, chunks, llm_client, sample_size=sample_size)
        report.verdicts = judged.verdicts
        report.judged = judged.judged
        report.skipped_uncited = judged.skipped_uncited

    return report


# ---------------------------------------------------------------------------
# The generic shape process, applied to ontology instances
# ---------------------------------------------------------------------------


# Applied to every instance whatever the domain, and whether or not a profile
# was supplied. Without these the shape check on a profile-less run examined
# nothing at all and reported `ran: false` — indistinguishable in a report from
# a clean pass, which is the failure that made the check unconditional in the
# first place. Phase 2's requirement shapes already had this fallback; Phase 1
# did not.
#
# Deliberately minimal. Each is a property without which an instance cannot be
# used at all, in any domain — not a house style, and nothing that needs to know
# what the document is about. Anything domain-specific belongs in a profile.
def _universal_violations(ontology):
    from phases.shape_check import Violation

    found = []
    for concept in ontology.concept_types:
        seen = {}
        for instance in concept.instances:
            # A user assertion records what the document does not say, so it has
            # no citation and no obligation to look like an extracted instance.
            if instance.source != SOURCE_DOCUMENT:
                continue

            name = str(instance.name or "").strip()
            if not name:
                found.append(Violation(
                    item_id=instance.instance_id, kind=concept.name, name="",
                    reason="no name — nothing can reference this instance",
                ))
                continue

            key = name.casefold()
            if key in seen:
                found.append(Violation(
                    item_id=instance.instance_id, kind=concept.name, name=name,
                    reason=f"duplicate name within {concept.name} — two instances that "
                           f"cannot be told apart (first was {seen[key]})",
                ))
                continue
            seen[key] = instance.instance_id

            if instance.source_chunk is None:
                found.append(Violation(
                    item_id=instance.instance_id, kind=concept.name, name=name,
                    reason="no verified citation — cannot be traced back to a passage",
                ))

    return found


def shape_report_for(ontology, profile, tracker=None):
    """Phase 1's adapter onto `phases.shape_check`.

    `check_shapes` above returns the raw violations and stays as it is, because
    several callers already read that shape. This wraps it in the common report
    every phase now produces — same fields, same review-flag wording, same
    recording to the run tracker — so a reader comparing two phases' findings is
    comparing like with like.
    """
    from phases.shape_check import ShapeReport, Violation, record

    rules = getattr(profile, "shape_rules", None) or {}
    report = ShapeReport(phase="phase1")

    # Universal first, so a run with no profile still checks something and the
    # report can say what it checked rather than reporting an empty pass.
    universal = _universal_violations(ontology)
    report.violations.extend(universal)
    report.rules_applied.append("universal")
    report.checked += sum(
        1 for c in ontology.concept_types for i in c.instances if i.source == SOURCE_DOCUMENT
    )

    if rules:
        # Only instances whose concept maps to a bucket with rules are examined;
        # `check_shapes` already applies that mapping, so the count here is the
        # population it actually considered.
        buckets = set()
        for concept in ontology.concept_types:
            bucket = _bucket_for(concept.name, profile)
            if bucket and rules.get(bucket):
                buckets.add(bucket)
        # `checked` counts instances, and the universal pass already counted
        # every one of them; adding the profile-covered subset again would
        # report more instances checked than the ontology contains.
        report.rules_applied = ["universal"] + sorted(buckets)

        raw = check_shapes(ontology, profile)
        # Reuse the wording this module already produces: it names the bucket and
        # quotes the rule that was broken, which a generic phrasing cannot.
        report.messages = TypeCheckReport(shape_violations=raw).review_flags()

        for violation in raw:  # noqa: B007 - appended below
            report.violations.append(
                Violation(
                    item_id=violation.instance_id,
                    kind=violation.bucket,
                    name=violation.name,
                    reason=violation.reason,
                )
            )

    return record(report, tracker)
