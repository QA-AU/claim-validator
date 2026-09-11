"""A deterministic, non-LLM safety net for one specific judge failure mode —
see issue #4: the entailment judge sometimes compares a claim's number to a
document's threshold number literally ("3% is not 0.1%, so contradicts")
instead of asking whether the claim's value lands on the correct side of the
threshold. Three rounds of prompt engineering (`phases/entailment.py`'s
`_THRESHOLD_PROCEDURE`) reduced but did not eliminate this — the model can
walk the procedure to the right answer and then override itself. Code that
does the arithmetic can't do that.

This module does NOT try to be a general numeric-reasoning engine. It
recognizes exactly one shape and abstains (returns `None`, leaving the LLM's
own verdict untouched) on everything else:

    the CLAIM states one number and one binary outcome word for it
    ("a 3% difference FAILS the page"), and
    a CITED PASSAGE states, in one sentence, a comparison operator + number
    + the same kind of outcome word ("FAILS when more than 0.1%...").

That narrowness is deliberate, not a shortcut to fix later:

- A sentence with a number but no outcome word ("the limit is 10 per
  second") never becomes a rule — this is what keeps a plain fixed-value
  fact (a real, different-value conflict the LLM already gets right) from
  ever being touched by this module.
- A claim naming two or more numbers is abstained on rather than guessed at
  — a compound claim is issue #3's territory, not this one's.
- Spelled-out numbers ("three", "once") are not recognized — only digits.
  A retry-count claim like "retry up to three times" is therefore left
  entirely to the LLM, same as today.

Wired into `claimvalidator/pipeline.py`'s per-claim loop, not into
`phases/entailment.py` — this is additive correction on top of the judge's
output, not a change to the judge itself, so it lives in the "everything
new" layer and leaves the reused judge code alone.
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

VERDICT_ENTAILS = "entails"
VERDICT_CONTRADICTS = "contradicts"

# float() already normalizes "0.1" / ".1" / "00.1" to the identical value —
# this regex's only job is capturing a valid numeric literal shape (with
# optional thousands separators and a trailing "%"), not parsing it.
_NUMBER_RE = re.compile(r'(?<!\w)(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\.\d+)\s*%?')

_POSITIVE_OUTCOME_WORDS = {
    "pass", "passes", "passed", "allow", "allowed", "accept", "accepted",
    "succeed", "succeeds", "success", "valid", "ok", "permitted", "granted",
}
_NEGATIVE_OUTCOME_WORDS = {
    "fail", "fails", "failed", "reject", "rejected", "deny", "denied",
    "block", "blocked", "invalid", "error", "errors", "forbidden",
    "refuse", "refused",
}

# Ordered most-specific-phrase-first so "no more than" matches before the
# "more than" it contains as a substring.
_COMPARISONS: List[Tuple[str, str]] = [
    (r"at\s+or\s+below", "<="), (r"at\s+or\s+above", ">="),
    (r"no\s+more\s+than", "<="), (r"no\s+less\s+than", ">="),
    (r"at\s+least", ">="), (r"at\s+most", "<="),
    (r"or\s+more", ">="), (r"or\s+above", ">="),
    (r"or\s+less", "<="), (r"or\s+fewer", "<="),
    (r"up\s+to", "<="),
    (r"greater\s+than", ">"), (r"more\s+than", ">"),
    (r"exceeds?", ">"), (r"exceeding", ">"),
    (r"above", ">"), (r"over", ">"),
    (r"less\s+than", "<"), (r"fewer\s+than", "<"),
    (r"below", "<"), (r"under", "<"),
]

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _parse_number(text: str) -> float:
    return float(text.replace(",", ""))


def _outcome_words_in(text: str) -> List[str]:
    words = re.findall(r"[a-zA-Z]+", text.lower())
    return [w for w in words if w in _POSITIVE_OUTCOME_WORDS or w in _NEGATIVE_OUTCOME_WORDS]


def _outcome_of(word: str) -> str:
    return "positive" if word in _POSITIVE_OUTCOME_WORDS else "negative"


@dataclass
class ThresholdRule:
    """One passage sentence's comparison rule: values satisfying `comparator
    value` produce `outcome`."""

    comparator: str  # "<" | "<=" | ">" | ">="
    value: float
    outcome: str  # "positive" | "negative"
    source_text: str

    def covers(self, x: float) -> bool:
        if self.comparator == "<":
            return x < self.value
        if self.comparator == "<=":
            return x <= self.value
        if self.comparator == ">":
            return x > self.value
        return x >= self.value  # ">="


@dataclass
class NumericOverride:
    verdict: str  # "entails" | "contradicts"
    explanation: str


# A companion sentence to a threshold statement often refers back to the
# number by pronoun rather than repeating it ("fails above 0.1%. Anything
# AT OR BELOW THAT is a pass.") — recognized only alongside an operator AND
# an outcome word already found in the same sentence, so this doesn't turn
# every unrelated "it"/"that" into a false rule.
_ANAPHORA_RE = re.compile(r"\bthat\b|\bthis\b", re.IGNORECASE)


def _extract_threshold_rules(text: str) -> List[ThresholdRule]:
    """One rule per sentence carrying BOTH a comparison operator AND a
    recognized outcome word, with the number either stated in that sentence
    or carried forward from the most recent number seen (for a companion
    sentence that refers back to it by pronoun rather than repeating it).
    See the module docstring for why a sentence missing the operator or the
    outcome word yields nothing regardless."""
    rules: List[ThresholdRule] = []
    last_number: Optional[float] = None

    for sentence in _SENTENCE_SPLIT_RE.split(text):
        outcome_words = _outcome_words_in(sentence)
        numbers_here = _NUMBER_RE.findall(sentence)

        if len(outcome_words) == 1:
            outcome = _outcome_of(outcome_words[0])
            for op_pattern, comparator in _COMPARISONS:
                m = re.search(op_pattern + r"\s*" + _NUMBER_RE.pattern, sentence, re.IGNORECASE)
                if m:
                    rules.append(ThresholdRule(
                        comparator=comparator, value=_parse_number(m.group(1)),
                        outcome=outcome, source_text=sentence.strip(),
                    ))
                    break
                if (last_number is not None and not numbers_here
                        and re.search(op_pattern, sentence, re.IGNORECASE)
                        and _ANAPHORA_RE.search(sentence)):
                    rules.append(ThresholdRule(
                        comparator=comparator, value=last_number,
                        outcome=outcome, source_text=sentence.strip(),
                    ))
                    break

        if numbers_here:
            last_number = _parse_number(numbers_here[-1])

    return rules


def _extract_claim_value_outcome(claim_text: str) -> Optional[Tuple[float, str]]:
    """Exactly one number and exactly one outcome word in the claim, or
    None. Two-or-more numbers (likely compound - see issue #3) or zero/two+
    recognized outcome words abstains rather than guesses."""
    numbers = _NUMBER_RE.findall(claim_text)
    if len(numbers) != 1:
        return None
    outcome_words = _outcome_words_in(claim_text)
    if len(outcome_words) != 1:
        return None
    return _parse_number(numbers[0]), _outcome_of(outcome_words[0])


def check_numeric_consistency(claim_text: str, passages: List[str]) -> Optional[NumericOverride]:
    """The deterministic answer for a numeric-threshold claim, or None to
    abstain and leave whatever verdict the judge already reached alone.

    Fires only when the claim names exactly one value+outcome and the
    passages contain exactly one threshold rule whose region contains that
    value - anything else (no rule, multiple candidate rules, an
    unrecognized comparison phrase) is left to the judge, deliberately.
    """
    claim_value_outcome = _extract_claim_value_outcome(claim_text)
    if claim_value_outcome is None:
        return None
    value, claim_outcome = claim_value_outcome

    rules: List[ThresholdRule] = []
    for passage in passages:
        rules.extend(_extract_threshold_rules(passage))
    if not rules:
        return None

    covering = [r for r in rules if r.covers(value)]
    if len(covering) != 1:
        return None  # no coverage, or ambiguous multi-rule coverage - abstain

    rule = covering[0]
    consistent = rule.outcome == claim_outcome
    verdict = VERDICT_ENTAILS if consistent else VERDICT_CONTRADICTS
    relation = "consistent with" if consistent else "the opposite of"
    explanation = (
        f"Deterministic threshold check: the passages state '{rule.source_text}' "
        f"({rule.comparator} {rule.value:g} -> {rule.outcome}); the claim's value "
        f"{value:g} falls in that region, which is {relation} the outcome the "
        f"claim states."
    )
    return NumericOverride(verdict=verdict, explanation=explanation)
