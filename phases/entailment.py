"""Does the cited passage actually *support* the claim?

Provenance answers **where a claim came from**. It cannot answer **whether that
passage says it**. Those are different questions, and only the second catches a
requirement that cites real instances in real chunks and still asserts something
the document contradicts.

The defect that motivated this module (todo/12) passed every existing check:

    requirement:  another merchant's order returns 404 order_not_found
    cited:        endpoint:get-orders-id      (chunk 1)  <- exists
                  error-code:order-not-found  (chunk 1)  <- exists
                  error-code:wrong-merchant   (chunk 0)  <- exists
    chunk 0 says: a different merchant's token returns 403 wrong_merchant
    chunk 1 says: an unknown id returns 404 order_not_found

Every citation is correct. The *inference* is not: the scenario came from one
passage and the status code from another, and the resulting rule appears in
neither. The run reported 39/39 traceable with zero review flags, because
everything it verified genuinely was correct.

### The verdicts

    entails        the passages state this, or it follows directly from them
    mentions_only  the passages are about these things but do not establish
                   the claim  <- the verdict that catches this class of defect
    contradicts    the passages state something incompatible with the claim
    no_evidence    the passages have nothing to do with the claim

`mentions_only` is the load-bearing one. A binary supported/unsupported split
would push the 403/404 case toward "supported", because the passages *are* about
exactly those endpoints and error codes. Naming the middle state is what makes
it visible.

### What this module refuses to do

**Unjudged is not wrong.** A requirement with no citations cannot be judged
against passages that do not exist; it is reported as unjudgeable, never as
failing. An unreadable or missing verdict defaults to `entails` for the same
reason the type checker defaults to `ok` — a broken judge must not manufacture
accusations.

**It reads only the cited chunks.** Not the whole document, not retrieval — the
question is precisely whether *the passages this claim points at* support it.
Widening the evidence would answer a different and easier question.

### Cost

One call per batch of requirements, times `JUDGE_RUNS` — three by default,
because a single run is not stable enough to act on. Sweeping the judge five
times over identical input moved the verdict on roughly a quarter of
requirements on both documents measured, and two separate single-run
contradiction lists failed to reproduce at all. Three runs recover a five-run
majority ~95% of the time; the constant carries the figures.

That makes this the one step in the pipeline that deliberately pays a multiple
for reliability. It is affordable because it is also the cheapest step: it reads
only the cited passages, never the document.

Batched from the start because Phase 3 taught the lesson the expensive way: a
single call carrying many items runs past the output ceiling and returns JSON
cut in half.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from phases.phase1_rag_indexer import CHUNK_SIZE

logger = logging.getLogger(__name__)

VERDICT_ENTAILS = "entails"
VERDICT_MENTIONS_ONLY = "mentions_only"
VERDICT_CONTRADICTS = "contradicts"
VERDICT_NO_EVIDENCE = "no_evidence"

VERDICTS = (VERDICT_ENTAILS, VERDICT_MENTIONS_ONLY, VERDICT_CONTRADICTS, VERDICT_NO_EVIDENCE)

# Chunks must reach the judge whole. Truncating them below the chunk size makes
# the judge report missing evidence that is merely off-screen — the exact bug
# that produced five false weak-citations in type_check before it was fixed.
JUDGE_CHUNK_CHARS = CHUNK_SIZE + 200

# Requirements per call. Small: each carries several passages, and a truncated
# reply loses the batch.
JUDGE_BATCH = 3

# Below this share of judged requirements entailed, the set is not dependable as
# a description of the document, whatever its traceability says.
LOW_ENTAILMENT = 0.8

# How many times the whole set is judged before a verdict is reported.
#
# Three, and the number is measured rather than chosen. Sweeping the judge five
# times over identical input — same requirements, same passages, same model —
# moved the verdict on 23% of requirements on the orders set and 22% on the
# GitHub set. Two separate single-run contradiction lists failed to reproduce at
# all: the documented "zero id returns 400" false positive never recurred in
# five runs, and the GitHub run's two contradictions came back zero five times
# out of five. The only contradictions that survived repetition anywhere were
# the two that are the real 403/404 defect.
#
# Three is the point where that stops mattering. Taking the majority of three
# runs and comparing it against the five-run majority, across all ten three-run
# subsets, the short sweep recovers the long one's answer 94% of the time on
# orders and 95% on GitHub — at 60% of the cost. See todo/13.
#
# Set `runs=1` to get the old single-pass behaviour back; nothing else changes.
JUDGE_RUNS = 3

# Ascending order of accusation, used only to break a tie no majority resolved.
# A judge that cannot agree with itself must not be the reason a requirement is
# called wrong, so the least accusatory verdict on the table is the one
# reported. The disagreement is never hidden: an undecided verdict is a problem
# whatever label it carries, and it names every verdict that was seen.
_ACCUSATION_ORDER = (
    VERDICT_ENTAILS,
    VERDICT_MENTIONS_ONLY,
    VERDICT_NO_EVIDENCE,
    VERDICT_CONTRADICTS,
)

# For per-clause re-verification of a compound claim that already reads
# entails (issue #17) — deliberately separate from claim_retrieval.py's own
# `_split_claim_into_clauses` (issue #3), which only fires on a comma/
# semicolon before "and" and is scoped that conservatively because an
# extra split there costs one free local search (see that function's own
# docstring). An extra split here costs a real judge call, but only ever
# on a claim that already scored entails — bounded, so this splitter can
# afford to be looser: any plain " and ", no punctuation required. Not
# reused from claim_retrieval.py because phases/ never imports from
# claimvalidator/ — this stays in the layer both modules can reach.
_VERIFICATION_AND_RE = re.compile(r"\s+and\s+")
_VERIFICATION_PROTECTED_SPAN_RE = re.compile(r"`[^`]*`|\"[^\"]*\"")


def _split_into_verification_clauses(claim_text: str) -> List[str]:
    """Candidate sub-clauses for re-verification, or `[]` when the claim
    doesn't look compound by this (loose) rule — never `[claim_text]`, so a
    caller can tell "not compound" apart from "compound, one clause" (which
    cannot happen) without a length check."""
    protected = [m.span() for m in _VERIFICATION_PROTECTED_SPAN_RE.finditer(claim_text)]

    def _inside_protected(pos: int) -> bool:
        return any(start <= pos < end for start, end in protected)

    pieces, cursor = [], 0
    for m in _VERIFICATION_AND_RE.finditer(claim_text):
        if _inside_protected(m.start()):
            continue
        pieces.append(claim_text[cursor:m.start()].strip())
        cursor = m.end()
    pieces.append(claim_text[cursor:].strip().rstrip(".!?"))
    pieces = [p for p in pieces if p]
    return pieces if len(pieces) > 1 else []


# The process name these settings are stored under. Every value below is a
# working default; the database only ever overrides. See settings_registry.
SETTINGS_PROCESS = "entailment"

DEFAULT_SETTINGS: Dict[str, Any] = {
    # Re-judge on a stronger model where three runs could not agree. Cheap
    # because it is rare — undecided verdicts were 0 of 39 on the orders set.
    "escalate_undecided": True,
    # Re-judge a *contradiction* that only a bare majority supported. This is
    # the case the sweep actually caught: DELETE-ORDERS-ID-EDGECASE-001 came
    # back contradicts 2/3, where five runs put the majority at entails. A
    # contradiction is the pipeline's most actionable output and the most
    # expensive to get wrong, so a split one is worth a better model's opinion.
    "escalate_split_contradictions": True,
    # Re-judge an *entailment* that only a bare majority supported — issue
    # #17. Found live: a claim that a refactor "prevents race conditions"
    # landed entails 2/3 (one of three runs disagreed), the majority citing
    # the code's own comment asserting the same conclusion rather than the
    # SQL's actual semantics. mentions_only/no_evidence stay excluded on
    # split votes — those are still non-committal findings nothing acts on
    # the difference of — but entails is exactly as actionable, and exactly
    # as costly to get wrong, as a contradiction once a caller is asking
    # "did this actually fix the bug" rather than "is this claim true of a
    # spec."
    "escalate_split_entails": True,
    # Re-check a compound claim clause by clause once it reads entails as a
    # whole sentence — the second finding from issue #17. A true clause and
    # a false clause sharing one sentence can both land on the correct
    # per-word evidence and still produce a single, unanimous, wrong
    # `entails` for the sentence as a whole (see docs/refactor-accuracy-
    # demo/'s C1: "replaced jsonwebtoken with jose ... and better
    # cryptographic defaults" — true swap, false "better defaults" claim,
    # 3/3 entails regardless). Bounded cost: only ever runs on a claim
    # already scored entails, and only when it actually looks compound.
    "verify_entailed_clauses": True,
    # Which tier to escalate to. Tiers, never model ids: see phase1_model_config.
    "escalation_tier": "m",
    # The stronger model is judged by consensus too. A single pass would answer
    # an unstable verdict with another unstable verdict, which is the mistake
    # this whole item exists to stop making.
    "escalation_runs": JUDGE_RUNS,
    # How many times the whole set is judged. The measurement behind the 3 is on
    # JUDGE_RUNS; it is here as well because it is the pipeline's main
    # cost/reliability dial and a run should record which value it paid for.
    "judge_runs": JUDGE_RUNS,
    # Requirements per call. Not free to raise: a truncated reply loses a whole
    # batch, which is why it is small.
    "judge_batch": JUDGE_BATCH,
    # Below this share entailed, the set is flagged as not dependable.
    "low_entailment": LOW_ENTAILMENT,
    # After judging, retrieve for the claims that came back unsupported and ask
    # again if retrieval finds passages the judge did not see. Corrects a
    # citation defect — a claim the document does support, pointed at the wrong
    # chunk — which otherwise reads identically to a claim the document does not
    # support. Costs one retrieval and one judging call per batch of failures,
    # and only runs when a searcher is available.
    "recheck_unsupported": True,
}


@dataclass
class EntailmentVerdict:
    """One requirement, judged against the passages it cites."""

    requirement_id: str
    verdict: str = VERDICT_ENTAILS
    reason: str = ""
    cited_chunks: List[int] = field(default_factory=list)
    judged: bool = True

    # How many runs backed the reported verdict, out of how many judged this
    # requirement at all. A run whose batch failed does not count against
    # agreement — it did not disagree, it did not answer.
    agreement: int = 1
    runs_judged: int = 1
    # Every verdict this requirement drew, with counts. Present so a reader can
    # see the disagreement rather than only its resolution.
    verdicts_seen: Dict[str, int] = field(default_factory=dict)

    # Set when a stronger model was asked to settle this one. `escalated_from`
    # keeps the cheap model's answer: a verdict that changed on escalation is
    # the most interesting row in the report, and overwriting it would erase
    # the evidence that escalation did anything.
    escalated: bool = False
    escalated_from: str = ""
    escalation_model: str = ""

    # Set when the claim turned out to be supported by a passage it does not
    # cite: the chunks it did cite, and the ones that actually support it. A
    # citation defect reads differently from a content defect and the fix is
    # different — correct the citation rather than delete the requirement.
    miscited_from: List[int] = field(default_factory=list)
    better_chunks: List[int] = field(default_factory=list)

    # How this verdict was reached — "majority_vote" (the default, `runs`
    # repeated samples plus a vote) or "logprob" (one call, confidence read
    # from the model's own token probabilities instead of from agreement
    # across repeats — see claimvalidator/logprob_judge.py). `confidence` is
    # only set by the latter: a majority vote's `agreement`/`runs_judged`
    # already says what it needs to, and a bucketed 0/3-3/3 count is not the
    # same kind of number as a continuous probability, so the two are kept
    # in separate fields rather than one overloaded to mean either.
    method: str = "majority_vote"
    confidence: Optional[float] = None

    # Set when a compound claim that read `entails` as a whole sentence was
    # re-checked clause by clause (issue #17's second finding) — one entry
    # per clause: {"clause": ..., "verdict": ..., "reason": ...}. Empty for
    # every claim this step didn't apply to (not compound, or not entails
    # in the first place). Never collapsed away: a claim whose overall
    # verdict changed because of this should show exactly which clause did
    # it, the same transparency instinct as `verdicts_seen` above.
    clause_verdicts: List[Dict[str, str]] = field(default_factory=list)

    @property
    def decided(self) -> bool:
        """Did a strict majority of the runs that answered agree?"""
        return self.agreement * 2 > self.runs_judged

    @property
    def unanimous(self) -> bool:
        return self.runs_judged > 0 and self.agreement == self.runs_judged

    @property
    def is_problem(self) -> bool:
        # An undecided verdict is a problem even when the label is `entails`.
        # Consensus exists to resolve disagreement; where it cannot, saying so
        # is the honest output, and a tie broken toward `entails` would
        # otherwise vanish from the report entirely.
        return self.judged and (self.verdict != VERDICT_ENTAILS or not self.decided)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "verdict": self.verdict,
            "reason": self.reason,
            "cited_chunks": self.cited_chunks,
            "judged": self.judged,
            "agreement": self.agreement,
            "runs_judged": self.runs_judged,
            "decided": self.decided,
            "verdicts_seen": self.verdicts_seen,
            "escalated": self.escalated,
            "escalated_from": self.escalated_from,
            "escalation_model": self.escalation_model,
            "miscited_from": self.miscited_from,
            "better_chunks": self.better_chunks,
            "method": self.method,
            "confidence": self.confidence,
            "clause_verdicts": self.clause_verdicts,
        }


@dataclass
class EntailmentReport:
    verdicts: List[EntailmentVerdict] = field(default_factory=list)
    requirements_total: int = 0
    failed_batches: int = 0
    runs: int = 1
    # The model doubtful verdicts were escalated to. Set only once a verdict
    # actually came back from it — an escalation that failed every call must not
    # read as one that happened.
    escalated_model: str = ""
    # Escalation calls that failed, kept apart from `failed_batches`. Folding
    # them together made a clean main run report three failed batches, which
    # reads as "requirements went unchecked" when in fact all 39 were judged and
    # only the optional second opinion was lost.
    escalation_failed_batches: int = 0
    # What the settings resolved to and where each value came from, so a run
    # can explain its own behaviour after the rows have changed.
    settings: Dict[str, Any] = field(default_factory=dict)
    # Carried rather than read from the constant, so `entailment_is_low` states
    # the threshold it was judged against.
    low_entailment: float = LOW_ENTAILMENT

    def _of(self, verdict: str) -> List[EntailmentVerdict]:
        return [v for v in self.verdicts if v.judged and v.verdict == verdict]

    @property
    def judged(self) -> List[EntailmentVerdict]:
        return [v for v in self.verdicts if v.judged]

    @property
    def unjudgeable(self) -> List[EntailmentVerdict]:
        """Requirements with nothing to judge against — not accusations."""
        return [v for v in self.verdicts if not v.judged]

    @property
    def entailed(self) -> List[EntailmentVerdict]:
        return self._of(VERDICT_ENTAILS)

    @property
    def mentions_only(self) -> List[EntailmentVerdict]:
        return self._of(VERDICT_MENTIONS_ONLY)

    @property
    def contradicted(self) -> List[EntailmentVerdict]:
        return self._of(VERDICT_CONTRADICTS)

    @property
    def no_evidence(self) -> List[EntailmentVerdict]:
        return self._of(VERDICT_NO_EVIDENCE)

    @property
    def entailment_rate(self) -> Optional[float]:
        """Share of judged requirements the passages actually support.

        None when nothing could be judged — which is not the same as 0.0, and
        must not be rendered as a failing score.
        """
        if not self.judged:
            return None
        return len(self.entailed) / len(self.judged)

    @property
    def has_contradictions(self) -> bool:
        return bool(self.contradicted)

    @property
    def escalated(self) -> List[EntailmentVerdict]:
        """Verdicts a stronger model was asked to settle."""
        return [v for v in self.verdicts if v.escalated]

    @property
    def overturned(self) -> List[EntailmentVerdict]:
        """Escalated verdicts the stronger model actually changed.

        The most interesting rows in the report: each one is a finding the cheap
        model would have reported differently.
        """
        return [v for v in self.escalated if v.escalated_from != v.verdict]

    @property
    def undecided(self) -> List[EntailmentVerdict]:
        """Judged, but no majority of the runs agreed on what the verdict is."""
        return [v for v in self.judged if not v.decided]

    @property
    def unstable(self) -> List[EntailmentVerdict]:
        """Judged, decided, but not every run agreed.

        Reported as a statistic rather than a flag. Around a quarter of
        requirements land here on both documents measured, and consensus exists
        precisely to absorb them — flagging them all would hand back the noise
        this is meant to remove.
        """
        return [v for v in self.judged if not v.unanimous and v.decided]

    def review_flags(self) -> List[str]:
        flags: List[str] = []

        # Ordered by severity: a contradicted requirement is a defect, an
        # unsupported one is a risk, an unjudged one is merely unknown.
        if self.contradicted:
            flags.append(
                f"{len(self.contradicted)} requirement(s) assert something their own "
                f"cited passages contradict"
            )

        if self.mentions_only:
            flags.append(
                f"{len(self.mentions_only)} requirement(s) cite passages that are about "
                f"the right things but do not establish the claim"
            )

        if self.no_evidence:
            flags.append(
                f"{len(self.no_evidence)} requirement(s) cite passages unrelated to the claim"
            )

        if self.undecided:
            flags.append(
                f"{len(self.undecided)} requirement(s) drew a different verdict in every "
                f"run and no majority settled it — the judge could not decide, which is "
                f"not the same as a defect"
            )

        if self.unjudgeable:
            flags.append(
                f"{len(self.unjudgeable)} requirement(s) cite no passage and could not be "
                f"checked — unknown, not wrong"
            )

        if self.failed_batches:
            flags.append(
                f"{self.failed_batches} judging batch(es) failed; those requirements are "
                f"unchecked"
            )

        if self.escalation_failed_batches and not self.escalated:
            # Distinct from the above on purpose: the requirements were judged,
            # and only the optional second opinion was lost.
            flags.append(
                f"escalation to a stronger model failed "
                f"({self.escalation_failed_batches} call(s)); the doubtful verdicts stand "
                f"as the first model left them"
            )

        return flags

    def to_dict(self) -> Dict[str, Any]:
        rate = self.entailment_rate
        return {
            "requirements_total": self.requirements_total,
            "judged": len(self.judged),
            "unjudgeable": len(self.unjudgeable),
            "entails": len(self.entailed),
            "mentions_only": len(self.mentions_only),
            "contradicts": len(self.contradicted),
            "no_evidence": len(self.no_evidence),
            "entailment_rate": round(rate, 4) if rate is not None else None,
            "entailment_is_low": rate is not None and rate < self.low_entailment,
            "has_contradictions": self.has_contradictions,
            "failed_batches": self.failed_batches,
            "runs": self.runs,
            # How much the judge agreed with itself. Worth reading alongside the
            # rate: a set where a quarter of verdicts moved between runs is
            # normal, and one where nothing moved is worth a second look.
            "undecided": len(self.undecided),
            "changed_between_runs": len(self.unstable),
            "escalated": len(self.escalated),
            "overturned": len(self.overturned),
            "escalated_model": self.escalated_model,
            "escalation_failed_batches": self.escalation_failed_batches,
            "settings": self.settings,
            "problems": [v.to_dict() for v in self.verdicts if v.is_problem],
            # Every verdict, not only the problems — a reader who wants to know
            # what happened to one specific requirement needs the entailed ones
            # too, and "problems" exists to be a short list, not a lookup table.
            "verdicts": [v.to_dict() for v in self.verdicts],
            "review_flags": self.review_flags(),
        }


def _parse_verdicts(response: str) -> List[Dict[str, Any]]:
    if not response:
        return []
    fenced = re.search(r"```(?:json)?\s*(.+?)```", response, re.DOTALL)
    if fenced:
        response = fenced.group(1)
    match = re.search(r"\[.*\]", response, re.DOTALL)
    if not match:
        logger.warning(f"[Entailment] Unparseable response: {response[:120]!r}")
        return []
    try:
        data = json.loads(match.group())
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    except json.JSONDecodeError:
        logger.warning(f"[Entailment] Malformed JSON: {response[:120]!r}")
        return []


def _claim_of(requirement) -> str:
    """The assertion to judge — the requirement's actual testable content."""
    parts = [requirement.title]
    if requirement.expected_behavior:
        parts.append(f"Expected: {requirement.expected_behavior}")
    if requirement.criteria:
        parts.append("Criteria: " + "; ".join(requirement.criteria))
    return " | ".join(p for p in parts if p)


# claim-validator: the "different value" rule below was, on its own, both
# under- and over-firing on any claim stating a threshold or range rather
# than a fixed value — see issue #4. "the limit is 25/s" vs a document's
# "10/s" is a real conflict the original rule caught correctly; "a 3%
# difference fails" vs a document's "fails above 0.1%" is not a conflict at
# all (3% is on the failing side of that threshold), and the same rule was
# calling it one — restated almost verbatim in the model's own reason text,
# unmoved by a first attempt at this fix that stated the distinction as
# prose appended after the JSON-format instructions. That version also
# regressed a previously-correct verdict (a claim's own reasoning computed
# "0.5% exceeds 0.1%" and then concluded the wrong outcome from it). This
# version instead puts a forced, numbered arithmetic procedure directly
# inside the "contradicts" test itself — the decision point where the
# mistake happens — with the two exact failing cases worked through so the
# correct conclusion for each is spelled out, not left for the model to
# derive from a general rule.
_THRESHOLD_PROCEDURE = """A claim can state an outcome for a NUMBER the passages never name, while the
   passages state a THRESHOLD or boundary rule instead (e.g. passages: "fails
   when more than 0.1% differ"; claim: "a 3% difference fails"). Comparing
   the two numbers directly ("3% is not 0.1%, so contradicts") is WRONG here.
   Instead, for a claim naming a specific value against a passage stating a
   threshold/boundary rule for the same quantity, work through these steps
   in order before deciding:
     a. Find the passages' rule and which outcome it produces above/below
        the boundary.
     b. Apply that rule to the CLAIM's number to get the outcome the
        PASSAGES would produce for it.
     c. Compare that computed outcome to the outcome the CLAIM states for
        that number.
     d. Same outcome -> not a contradiction (continue to test 2-4 below).
        Different outcome -> "contradicts".
        STOP HERE once you reach (d). Do not re-examine the claim's number
        against the passages' boundary number a second time afterward — a
        boundary number (0.1% in the example below) is not "the value the
        passages give for this quantity" in the fixed-value sense, it is
        the cutoff the procedure above already used. Re-applying "the
        claim's number differs from the passages' number, so contradicts"
        AFTER finishing this procedure is exactly the mistake this
        procedure exists to prevent, and produces the wrong verdict even
        when steps (a)-(d) were followed correctly.
   Worked example, passages: "fails when more than 0.1% of pixels differ;
   at or below that is a pass":
     - claim "a 3% difference fails the page": (a) above 0.1% -> fails.
       (b) 3% > 0.1% -> the rule says fail. (c) claim says fail. (d) SAME
       outcome -> not a contradiction -> entails. FINAL — do not also
       flag "3% is a different number than 0.1%" as a conflict; 0.1% was
       the boundary, not a competing value for the same fact.
     - claim "a 0.5% difference passes the page": (a) above 0.1% -> fails.
       (b) 0.5% > 0.1% -> the rule says fail. (c) claim says pass.
       (d) DIFFERENT outcome -> contradicts. FINAL.
   This procedure applies only to a genuine threshold/boundary. A passage
   stating one FIXED value for a quantity — a single number the passages
   assert IS the value, not a cutoff above/below which something happens —
   still contradicts a claim stating a different fixed value for it
   (passages: "the limit is 10 per second"; claim: "the limit is 25 per second"
   — there is no boundary here, both numbers claim to BE the value, so
   compare them directly as before). If you are unsure whether a number is
   a boundary or a fixed value, ask: does the passage describe what
   happens ABOVE or BELOW it? If yes, it is a boundary — use the procedure
   above and stop at (d)."""


def _build_prompt(items) -> str:
    blocks = []
    for requirement, passages in items:
        cited = "\n".join(
            f"    [chunk {n}] {text[:JUDGE_CHUNK_CHARS]!r}" for n, text in passages
        )
        blocks.append(
            f'- id: "{requirement.id}"\n'
            f"  claim: {_claim_of(requirement)}\n"
            f"  cited_passages:\n{cited}"
        )

    return f"""Decide whether each claim below is supported by the passages it cites.

{chr(10).join(blocks)}

Work through these tests IN ORDER and stop at the first that applies. They
overlap by design — a passage that contradicts a claim also fails to establish
it — so the order decides the verdict, not which description sounds closest.

1. "contradicts"   — do the passages specify something INCOMPATIBLE with the
                     claim for the same case? A different status code, error
                     code, limit or default counts. Choose this even though such
                     a passage also fails to establish the claim.

                     If the claim states a different FIXED value for the same
                     quantity than the passages give (a status code, a limit,
                     a default), that alone is "contradicts".

                     If instead the claim names a number and the passages
                     state a THRESHOLD or boundary rule for that same
                     quantity (not a fixed value), do not compare the two
                     numbers directly — follow this procedure first:
                     {_THRESHOLD_PROCEDURE}

                     If the claim restates a passage's rule with a WEAKER
                     quantifier — the passage says "all"/"every"/"any"/
                     "never"/"no X" and the claim says "some"/"most"/"many",
                     or the reverse (loosening a restriction the same way) —
                     that is also "contradicts", even though the weaker
                     statement is technically true whenever the stronger one
                     is. Do not reason "all implies some, so this follows" —
                     that reading drops the exact fact the passage actually
                     asserts (no exceptions exist) and replaces it with a
                     different, weaker claim (exceptions might exist). A
                     document that states a rule with no exceptions and a
                     claim that describes it as holding only "usually" or
                     "mostly" are not the same statement.
2. "no_evidence"   — is the SUBJECT the claim is about (the field, endpoint,
                     process, or mechanism it names — not the specific detail
                     it asserts, the underlying thing itself) absent from the
                     passages? If the passages never bring that subject up at
                     all, under any wording, that's this verdict. If the
                     passages are on an unrelated topic entirely, that also
                     lands here.
3. "mentions_only" — the passages DO discuss that same subject — the same
                     field, endpoint, process, or mechanism the claim names —
                     but are simply SILENT on the SPECIFIC detail, value, or
                     added behavior the claim asserts about it. The subject
                     is there; the particular thing the claim says about it
                     is not confirmed or denied. This applies even when that
                     specific added detail's exact wording never appears —
                     what matters is whether the subject itself is covered,
                     not whether every word of the claim is.
4. "entails"       — the passages state the claim, or it follows directly.

                     A comment, docstring, or identifier name that merely
                     ASSERTS the same conclusion the claim makes — a
                     comment reading "prevents X" for a claim asserting the
                     code prevents X, a function named handlesXSafely for a
                     claim that it handles X safely — is not itself
                     sufficient evidence for this. Trace the actual
                     operations near that comment or name (the real data
                     flow, the conditions actually checked) and confirm
                     they independently deliver the claimed effect. A
                     batched SQL UPDATE next to a comment claiming it
                     "prevents race conditions" does not entail a claim
                     that race conditions are prevented unless the
                     passages also show a lock, a transaction, or a
                     conditional check tied to the value being updated —
                     combining several writes into one statement is not
                     the same thing as making the value they write
                     race-safe. Without that mechanism visible, the
                     correct verdict is "mentions_only": the subject (the
                     update) is covered, the added property the claim
                     asserts about it (safety under concurrency) is not
                     confirmed.

For each item return an object:
- id: the id exactly as given
- verdict: exactly one of "contradicts", "no_evidence", "mentions_only", "entails"
- reason: a short phrase quoting the deciding words, unless the verdict is "entails"

Judge ONLY against the passages shown. Common industry practice is not evidence:
if a claim states what an API usually does but this document specifies something
else, that is "contradicts". Between "no_evidence" and "mentions_only", ask two
separate questions in order. First: do the passages discuss the SAME subject —
the same field, process, mechanism, or entity — the claim is about, regardless
of wording? If no, that's "no_evidence", no matter how much the passages say
about other things. If yes, ask a second question: do the passages confirm the
SPECIFIC detail, value, or added behavior the claim asserts about that subject?
If they're silent on it, that's "mentions_only" — the subject is covered, the
added detail is not. Only land on "no_evidence" when the subject itself never
comes up — not merely because the claim's specific added detail isn't named
verbatim. Do not fill gaps from your own knowledge of how APIs normally behave.

Return ONLY a JSON array."""


def record_assumptions(requirements, report: EntailmentReport) -> int:
    """Mark requirements the document does not state as **assumptions**.

    `mentions_only` is not a defect verdict. Seventeen of the thirty-nine
    requirements in the first live run asserted behaviour the document is simply
    silent on — what happens at `limit=0`, `limit=-1`, `limit=abc` when the spec
    defines only the maximum. As *test design* that is often exactly right; a
    tester should probe those. What was wrong was **presentation**: they shipped
    as documented requirements, indistinguishable from the ones the document
    actually states.

    So they are relabelled rather than deleted. The test survives, and the claim
    it makes about its own basis becomes true.

    `contradicts` is deliberately *not* treated this way. A claim the document
    conflicts with is not an assumption on top of the document — it is wrong
    about the document, and calling it an assumption would launder a defect.

    Returns the number marked.
    """
    basis_by_id = {
        v.requirement_id: v.reason
        for v in report.verdicts
        if v.judged and v.verdict in (VERDICT_MENTIONS_ONLY, VERDICT_NO_EVIDENCE)
    }

    marked = 0
    for requirement in requirements:
        basis = basis_by_id.get(requirement.id)
        if basis is None:
            continue
        requirement.is_assumption = True
        requirement.assumption_basis = (
            basis or "The cited passages do not state this; it is assumed on top of them."
        )
        marked += 1

    if marked:
        logger.info(
            f"[Entailment] Recorded {marked} requirement(s) as assumptions rather than "
            f"as statements of the document"
        )
    return marked


def _judge_once(
    requirements,
    chunks: List[str],
    llm_client,
    batch_size: int,
) -> EntailmentReport:
    """One full pass over the requirements. The primitive `judge_entailment`
    repeats; on its own it is a single sample and should be read as one."""
    report = EntailmentReport(requirements_total=len(requirements))

    judgeable = []
    for requirement in requirements:
        passages = [
            (n, chunks[n])
            for n in (requirement.source_chunks or [])
            if isinstance(n, int) and 0 <= n < len(chunks)
        ]
        if passages:
            judgeable.append((requirement, passages))
        else:
            # No citation means nothing to judge against. That is a traceability
            # finding, which the pipeline already reports; it is not evidence
            # that the claim is wrong.
            report.verdicts.append(
                EntailmentVerdict(requirement_id=requirement.id, judged=False)
            )

    for start in range(0, len(judgeable), batch_size):
        batch = judgeable[start : start + batch_size]

        try:
            response = llm_client.generate(_build_prompt(batch))
        except Exception as e:
            report.failed_batches += 1
            logger.error(f"[Entailment] Batch {start // batch_size + 1} failed: {e}")
            continue

        by_id = {}
        for item in _parse_verdicts(response):
            if item.get("id"):
                by_id[str(item["id"])] = item

        for requirement, passages in batch:
            raw = by_id.get(requirement.id)
            if raw is None:
                # An absent verdict is not a failing one.
                logger.debug(f"[Entailment] No verdict returned for {requirement.id}")
                continue

            verdict = str(raw.get("verdict", "")).strip().lower()
            if verdict not in VERDICTS:
                # An unreadable verdict must not accuse.
                verdict = VERDICT_ENTAILS

            report.verdicts.append(
                EntailmentVerdict(
                    requirement_id=requirement.id,
                    verdict=verdict,
                    reason=str(raw.get("reason", "")).strip(),
                    cited_chunks=[n for n, _ in passages],
                )
            )

    return report


def _consensus(passes: List[EntailmentReport]) -> EntailmentReport:
    """Reduce several passes to one report by majority verdict per requirement.

    Requirements are keyed by id across passes. A pass that failed to return a
    verdict for a requirement — a dropped batch, an unparseable reply — is not
    counted as a dissenting vote; it simply did not answer, and `runs_judged`
    records how many did.
    """
    merged = EntailmentReport(
        requirements_total=max(p.requirements_total for p in passes),
        failed_batches=sum(p.failed_batches for p in passes),
        runs=len(passes),
    )

    order: List[str] = []
    judged_by_id: Dict[str, List[EntailmentVerdict]] = {}
    unjudgeable: Dict[str, EntailmentVerdict] = {}

    for report in passes:
        for verdict in report.verdicts:
            rid = verdict.requirement_id
            if rid not in judged_by_id and rid not in unjudgeable:
                order.append(rid)
            if verdict.judged:
                judged_by_id.setdefault(rid, []).append(verdict)
                unjudgeable.pop(rid, None)
            elif rid not in judged_by_id:
                unjudgeable[rid] = verdict

    for rid in order:
        votes = judged_by_id.get(rid)
        if not votes:
            # Never judged in any pass: no citations, so nothing to judge
            # against. Unchanged by repetition, and still not an accusation.
            merged.verdicts.append(unjudgeable[rid])
            continue

        counts: Dict[str, int] = {}
        for vote in votes:
            counts[vote.verdict] = counts.get(vote.verdict, 0) + 1

        top = max(counts.values())
        tied = [v for v, c in counts.items() if c == top]
        # One winner, or the least accusatory of a tie.
        winner = min(tied, key=lambda v: _ACCUSATION_ORDER.index(v)
                     if v in _ACCUSATION_ORDER else len(_ACCUSATION_ORDER))

        # The reason is taken from a pass that actually reached the reported
        # verdict, so the quoted words and the label always agree. Synthesising
        # a summary across passes would produce a reason no run gave.
        spoke = next(v for v in votes if v.verdict == winner)

        merged.verdicts.append(
            EntailmentVerdict(
                requirement_id=rid,
                verdict=winner,
                reason=spoke.reason,
                cited_chunks=spoke.cited_chunks,
                judged=True,
                agreement=counts[winner],
                runs_judged=len(votes),
                verdicts_seen=dict(sorted(counts.items())),
            )
        )

    return merged


def _needs_escalation(verdict: EntailmentVerdict, settings) -> bool:
    """Is this verdict one a stronger model should be asked about?

    Three cases, all measured rather than guessed. An undecided verdict is the
    judge saying it does not know. A split contradiction is the judge making the
    pipeline's most actionable accusation without agreeing with itself — the
    exact shape of the one live error the three-run default still lets through.
    A split entailment (issue #17) is the same shape from the other direction:
    a confident confirmation the judge did not actually agree with itself on.
    mentions_only/no_evidence stay excluded even when split — those are already
    non-committal findings, and nothing downstream acts on the difference.
    """
    if not verdict.judged:
        return False
    if settings.get("escalate_undecided") and not verdict.decided:
        return True
    if (
        settings.get("escalate_split_contradictions")
        and verdict.verdict == VERDICT_CONTRADICTS
        and not verdict.unanimous
    ):
        return True
    if (
        settings.get("escalate_split_entails")
        and verdict.verdict == VERDICT_ENTAILS
        and not verdict.unanimous
    ):
        return True
    return False


def _build_escalation_client(tier: str):
    """A client on a stronger tier, or None if one cannot be made.

    Escalation is an improvement, not a precondition. If the stronger tier has
    no key or the SDK cannot be constructed, the run keeps the verdicts it
    already has and says so — refusing to report anything because the *optional*
    second opinion failed would be the wrong trade entirely.
    """
    try:
        from phase1_model_config import default_model
        from phases.cli_client import AnthropicClient

        return AnthropicClient(model=default_model(tier))
    except Exception as e:
        logger.warning(f"[Entailment] Cannot escalate to tier {tier!r} ({e}); keeping verdicts")
        return None


def _escalate(
    report: EntailmentReport,
    requirements_by_id: Dict[str, Any],
    chunks: List[str],
    batch_size: int,
    settings,
    escalation_client=None,
) -> None:
    """Re-judge the doubtful verdicts on a stronger model, in place."""
    candidates = [v for v in report.verdicts if _needs_escalation(v, settings)]
    if not candidates:
        return

    tier = str(settings.get("escalation_tier") or "").strip()
    client = escalation_client or _build_escalation_client(tier)
    if client is None:
        return

    model = getattr(client, "model", "") or f"tier {tier}"
    runs = max(1, int(settings.get("escalation_runs") or 1))
    logger.info(
        f"[Entailment] Escalating {len(candidates)} doubtful verdict(s) to {model} "
        f"({runs} run(s))"
    )

    subset = [requirements_by_id[v.requirement_id] for v in candidates
              if v.requirement_id in requirements_by_id]
    if not subset:
        return

    passes = [_judge_once(subset, chunks, client, batch_size) for _ in range(runs)]
    settled = _consensus(passes) if runs > 1 else passes[0]
    by_id = {v.requirement_id: v for v in settled.verdicts if v.judged}

    report.escalation_failed_batches += settled.failed_batches

    for original in candidates:
        better = by_id.get(original.requirement_id)
        if better is None:
            # The stronger model did not answer either. The cheap model's
            # verdict stands, still marked undecided if it was.
            continue

        was = original.verdict
        original.verdict = better.verdict
        original.reason = better.reason
        original.agreement = better.agreement
        original.runs_judged = better.runs_judged
        original.verdicts_seen = better.verdicts_seen
        original.escalated = True
        original.escalated_from = was
        original.escalation_model = model
        report.escalated_model = model
        if was != better.verdict:
            logger.info(
                f"[Entailment] {original.requirement_id}: {was} -> {better.verdict} "
                f"on {model}"
            )


def _verify_entailed_clauses(
    report: EntailmentReport,
    requirements_by_id: Dict[str, Any],
    chunks: List[str],
    batch_size: int,
    settings,
    llm_client,
) -> None:
    """Re-check a compound claim clause by clause once it reads `entails` as
    a whole sentence — issue #17's second finding. Never touches a claim
    that reads contradicts/mentions_only/no_evidence, and never touches a
    claim `_split_into_verification_clauses` doesn't consider compound.

    Each clause is judged against the same cited chunks the whole claim
    already had — no new retrieval, reusing `_judge_once` unchanged, a
    clause is just another one-line claim to it. A clause's own batch
    failing leaves the whole-claim verdict untouched rather than rolling up
    on incomplete data (same caution `_escalate` above already takes: a
    partial answer is not a mandate to overwrite a real one).
    """
    if not settings.get("verify_entailed_clauses"):
        return

    candidates = [v for v in report.verdicts if v.judged and v.verdict == VERDICT_ENTAILS]
    if not candidates:
        return

    for verdict in candidates:
        requirement = requirements_by_id.get(verdict.requirement_id)
        if requirement is None:
            continue
        full_claim = _claim_of(requirement)
        clauses = _split_into_verification_clauses(full_claim)
        if not clauses:
            continue

        # The full original sentence rides along as context (via
        # `expected_behavior`, which `_claim_of` already appends after the
        # clause) rather than judging the bare fragment alone. Found live:
        # splitting "the response contains the user's username and email
        # matching the account" at "and" leaves "email matching the
        # account..." with no subject of its own — a fragment that reads as
        # unconfirmable standalone even though the full sentence made the
        # shared subject obvious. The clause is still the thing being
        # verified; the full sentence is only there to resolve what a bare
        # fragment can't.
        pseudo_requirements = [
            SimpleNamespace(
                id=f"{verdict.requirement_id}::clause{i}",
                title=clause,
                expected_behavior=f'Read as part of the full original claim: "{full_claim}"',
                criteria=[],
                source_chunks=requirement.source_chunks,
            )
            for i, clause in enumerate(clauses)
        ]
        clause_report = _judge_once(pseudo_requirements, chunks, llm_client, batch_size)
        clause_by_id = {v.requirement_id: v for v in clause_report.verdicts if v.judged}
        if len(clause_by_id) < len(pseudo_requirements):
            continue  # a clause's own batch failed — leave the whole-claim verdict alone

        clause_verdicts = [
            {
                "clause": pr.title,
                "verdict": clause_by_id[pr.id].verdict,
                "reason": clause_by_id[pr.id].reason,
            }
            for pr in pseudo_requirements
        ]
        verdict.clause_verdicts = clause_verdicts

        results = [c["verdict"] for c in clause_verdicts]
        if all(r == VERDICT_ENTAILS for r in results):
            continue  # confirmed clause by clause — the whole-claim entails stands

        was = verdict.verdict
        verdict.verdict = VERDICT_CONTRADICTS if VERDICT_CONTRADICTS in results else VERDICT_MENTIONS_ONLY
        verdict.reason = "Compound claim, verified clause by clause: " + "; ".join(
            f"'{c['clause']}' -> {c['verdict']}" for c in clause_verdicts
        )
        logger.info(
            f"[Entailment] {verdict.requirement_id}: {was} -> {verdict.verdict} "
            f"on per-clause re-verification"
        )


def judge_entailment(
    requirements,
    chunks: List[str],
    llm_client,
    batch_size: Optional[int] = None,
    max_requirements: Optional[int] = None,
    runs: Optional[int] = None,
    db_session=None,
    escalation_client=None,
    settings=None,
) -> EntailmentReport:
    """Check each requirement against the passages it cites, `runs` times over,
    and report the verdict a majority of the runs agreed on.

    `requirements` is any iterable of objects with `id`, `title`,
    `expected_behavior`, `criteria` and `source_chunks` — a `TestRequirement`,
    or anything shaped like one.

    Repetition is the default because a single run is demonstrably not stable
    enough to act on — see `JUDGE_RUNS` for the measurement. `runs=1` restores
    the old behaviour exactly, including the report shape.

    Where the runs still cannot agree, a stronger model is asked. `db_session`
    is used only to resolve those escalation settings and to record which
    version of each was in force; without one the built-in defaults apply and
    the run says so.
    """
    requirements = list(requirements)
    if max_requirements is not None:
        requirements = requirements[:max_requirements]

    from phases.settings_registry import settings_for

    settings = settings_for(SETTINGS_PROCESS, DEFAULT_SETTINGS, settings, db_session)
    # An explicit argument still wins over a stored setting: a caller that named
    # a value meant it, and `runs=1` in a test must stay one run.
    if runs is None:
        runs = settings.get("judge_runs", JUDGE_RUNS)
    if batch_size is None:
        batch_size = settings.get("judge_batch", JUDGE_BATCH)

    runs = max(1, int(runs))
    passes = [_judge_once(requirements, chunks, llm_client, batch_size) for _ in range(runs)]
    report = passes[0] if runs == 1 else _consensus(passes)
    report.settings = settings.provenance() if hasattr(settings, "provenance") else {}
    report.low_entailment = settings.get("low_entailment", LOW_ENTAILMENT)

    requirements_by_id = {r.id: r for r in requirements}

    # Escalation reads the consensus, so it cannot run before it. Only ever
    # applied to verdicts the consensus left doubtful.
    _escalate(
        report,
        requirements_by_id,
        chunks,
        batch_size,
        settings,
        escalation_client=escalation_client,
    )

    # Reads whatever verdict escalation left in place — a claim escalation
    # just confirmed as entails is exactly as eligible for per-clause
    # re-verification as one that was always entails.
    _verify_entailed_clauses(report, requirements_by_id, chunks, batch_size, settings, llm_client)

    rate = report.entailment_rate
    logger.info(
        f"[Entailment] {len(report.judged)} judged over {runs} run(s), "
        f"{len(report.entailed)} entailed, {len(report.mentions_only)} mentions-only, "
        f"{len(report.contradicted)} contradicted"
        + (f", rate {rate:.0%}" if rate is not None else "")
    )
    if runs > 1:
        logger.info(
            f"[Entailment] {len(report.unstable)} verdict(s) changed between runs and were "
            f"settled by majority, {len(report.undecided)} could not be settled"
        )
    if report.escalated:
        logger.info(
            f"[Entailment] {len(report.escalated)} verdict(s) escalated to "
            f"{report.escalated_model}, {len(report.overturned)} changed as a result"
        )
    elif report.escalation_failed_batches:
        logger.warning(
            f"[Entailment] escalation failed ({report.escalation_failed_batches} call(s)); "
            f"the doubtful verdicts stand as the first model left them"
        )
    for flag in report.review_flags():
        logger.warning(f"[Entailment] {flag}")

    return report


# ---------------------------------------------------------------------------
# Looking for a better passage than the one that was cited
# ---------------------------------------------------------------------------
#
# The judge reads the cited passages and nothing else, deliberately: the
# question is whether *this* claim's own evidence supports it, and widening the
# evidence answers an easier one.
#
# That is right, and it leaves a real case unhandled. A requirement can cite the
# wrong passage while a passage that does support it sits elsewhere in the same
# document. The verdict "not supported" is then correct about the citation and
# misleading about the claim, and the fix a reader needs is not "delete this
# requirement" but "you cited the wrong chunk".
#
# So this runs *after* a verdict, only on the ones that failed, and it never
# overturns a judgement on its own: it retrieves for the claim, and if the
# passages it finds are ones the judge did not see, it asks again against those.
# A claim that is supported by neither is unchanged.

# Passages to retrieve when looking for a better one. Small: the point is the
# best few candidates, not a wider net that makes the second question easier
# than the first.
RECHECK_TOP_K = 4

# Only these verdicts are worth re-checking. A contradiction is a statement
# about the document, not about the citation — finding some other passage that
# agrees does not resolve it, and would let a real defect be argued away.
RECHECKABLE = (VERDICT_MENTIONS_ONLY, VERDICT_NO_EVIDENCE)


def recheck_against_better_passages(
    report: EntailmentReport,
    requirements,
    chunks: List[str],
    searcher,
    llm_client,
    batch_size: int = JUDGE_BATCH,
    runs: int = 1,
) -> int:
    """Re-judge unsupported claims against passages retrieval finds for them.

    Returns the number of verdicts that changed. Mutates `report` in place,
    recording the original verdict and the chunks that were tried, so a verdict
    that moved says what moved it.
    """
    by_id = {r.id: r for r in requirements}
    candidates = [
        v for v in report.verdicts
        if v.judged and v.verdict in RECHECKABLE and v.requirement_id in by_id
    ]
    if not candidates or searcher is None:
        return 0

    class _Reframed:
        """The requirement, pointed at the passages retrieval suggests."""

        def __init__(self, requirement, source_chunks):
            self.id = requirement.id
            self.title = requirement.title
            self.expected_behavior = getattr(requirement, "expected_behavior", "")
            self.criteria = getattr(requirement, "criteria", [])
            self.source_chunks = source_chunks

    reframed, tried = [], {}
    for verdict in candidates:
        requirement = by_id[verdict.requirement_id]
        try:
            retrieval = searcher.retrieve(_claim_of(requirement), top_k=RECHECK_TOP_K)
        except Exception as e:
            logger.warning(f"[Entailment] Could not retrieve for {requirement.id}: {e}")
            continue

        # Only passages the judge has not already seen. Re-asking about the same
        # chunks would just be a second sample of the first question.
        fresh = [n for n in (retrieval.indices or []) if n not in (verdict.cited_chunks or [])]
        if not fresh:
            continue
        tried[requirement.id] = fresh
        reframed.append(_Reframed(requirement, fresh))

    if not reframed:
        return 0

    logger.info(
        f"[Entailment] Re-checking {len(reframed)} unsupported claim(s) against passages "
        f"retrieval found for them"
    )
    second = judge_entailment(
        reframed, chunks, llm_client, batch_size=batch_size, runs=runs,
        settings={
            "escalate_undecided": False,
            "escalate_split_contradictions": False,
            "escalate_split_entails": False,
            "verify_entailed_clauses": False,
        },
    )

    better = {v.requirement_id: v for v in second.verdicts if v.judged}
    changed = 0
    for verdict in candidates:
        found = better.get(verdict.requirement_id)
        # Only an *improvement* is taken. A second look that agrees, or that
        # finds a contradiction elsewhere, leaves the original judgement alone —
        # this exists to correct a citation, not to shop for a kinder verdict.
        if found is None or found.verdict != VERDICT_ENTAILS:
            continue

        verdict.miscited_from = list(verdict.cited_chunks or [])
        verdict.verdict = VERDICT_ENTAILS
        verdict.reason = (
            f"Supported by chunk(s) {', '.join(str(n) for n in found.cited_chunks)}, "
            f"which the requirement does not cite. {found.reason}"
        )
        verdict.better_chunks = list(found.cited_chunks or [])
        changed += 1

    if changed:
        logger.info(
            f"[Entailment] {changed} claim(s) are supported by a passage they did not "
            f"cite — a citation defect, not a content defect"
        )
    return changed
