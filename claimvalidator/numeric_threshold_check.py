"""A deterministic, non-LLM safety net for two specific judge failure modes.

Shape 1 — see issue #4: the entailment judge sometimes compares a claim's
number to a document's threshold number literally ("3% is not 0.1%, so
contradicts") instead of asking whether the claim's value lands on the
correct side of the threshold. Three rounds of prompt engineering
(`phases/entailment.py`'s `_THRESHOLD_PROCEDURE`) reduced but did not
eliminate this — the model can walk the procedure to the right answer and
then override itself. Code that does the arithmetic can't do that. Fires
when the CLAIM states one number and one binary outcome word for it
("a 3% difference FAILS the page"), and a CITED PASSAGE states, in one
sentence, a comparison operator + number + the same kind of outcome word
("FAILS when more than 0.1%...").

Shape 2 — see issue #15: a claim that restates one of the document's own
comparator+number bounds, but changes the number, with no outcome word on
either side ("no more than 10 requests per second" restated as "no more
than 11"). This was originally left to the judge on the theory that a
plain fixed-value mismatch like this is easy for it to get right without
help — issue #15 found a live case where the judge's own stated reasoning
reached the correct conclusion and then emitted the opposite verdict
anyway. Fires when the claim states exactly one such bound and a passage
sentence states a bound with the same comparator, about what reads as the
same fact (most of the claim's own wording found inside the passage
sentence, once both numbers are stripped out).

Shape 3 — see issue #8: a claim stating a two-number RANGE ("a response
between 200ms and 500ms is acceptable") was previously abstained on
outright, same as any other claim naming two or more numbers — correct
for a genuinely compound claim (issue #3's territory), but not for a
single assertion that just happens to spell its bound as two numbers.
Fires only on an explicit "between X and Y" / "from X to Y" range phrase,
matched against a passage stating the identical range for what reads as
the same fact. Deliberately does NOT recognize a bare "X-Y" / "X to Y"
hyphen-joined form — that shape is genuinely ambiguous with a section
reference, a date range, or a version range, and telling those apart
needs more than this module currently attempts; left to the judge, same
as a spelled-out number. Also deliberately answers only "does the claim
restate the document's range," not "is the claim's range compatible with
it" — a claim range that is merely a subset of a wider passage range
(e.g. "300-400ms" inside a passage's stated "200-500ms") does not fire
either; that is a real, known gap this module abstains on rather than
guesses at.

This module does NOT try to be a general numeric-reasoning engine — each
shape recognizes one narrow pattern and abstains (returns `None`, leaving
the LLM's own verdict untouched) on everything else:

- A sentence with a number but no outcome word and no comparator phrase
  never becomes a shape-1 rule.
- A claim naming two or more numbers is abstained on rather than guessed
  at, unless it matches shape 3's own narrow range pattern exactly — a
  claim with a range PLUS a third unrelated number still abstains, same
  as any other multi-number claim (issue #3's territory).
- Spelled-out numbers ("three", "once") are not recognized — only digits.
  A retry-count claim like "retry up to three times" is therefore left
  entirely to the LLM, same as today.
- Shape 2 abstains outright on a comparator mismatch (a bound restated
  with "at least" where the passage said "at most") — telling those apart
  needs real reasoning about direction, which this module doesn't attempt.

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

# Deliberately NOT "passed" — found live, a real false positive: "unless
# `--update-baselines` was passed" (a flag being supplied) uses the exact
# same past-participle form as "the check passed" (an outcome), and there
# is no word-list way to tell those two senses apart. Losing that one form
# is the safe trade; "pass"/"passes" alone are far less ambiguous in
# practice ("the flag pass" and "the flag passes" are not idiomatic).
_POSITIVE_OUTCOME_WORDS = {
    "pass", "passes", "allow", "allowed", "accept", "accepted",
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
# A markdown heading line has no terminal punctuation, so without this a
# heading merges into the very next sentence as one "sentence" for outcome-
# word counting — found live: "## Pass and fail\n\nA page fails when more
# than 0.1%..." carries THREE outcome words ("Pass", "fail" from the
# heading, "fails" from the actual rule), which this module's own
# ambiguity guard (exactly one outcome word, or abstain) then correctly
# refused to touch — correct behavior, on text that shouldn't have been
# one "sentence" in the first place.
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+.*$")
_PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n")


def _sentences(text: str) -> List[str]:
    """Split into sentences, treating a markdown heading line and a blank
    paragraph break as hard boundaries even without terminal punctuation —
    a heading is a label, not a clause the words around it belong to."""
    text = _HEADING_RE.sub(" ", text)
    text = _PARAGRAPH_BREAK_RE.sub(". ", text)
    return _SENTENCE_SPLIT_RE.split(text)


def _parse_number(text: str) -> float:
    return float(text.replace(",", ""))


def _strip_markdown_emphasis(text: str) -> str:
    """Source briefs routinely bold the exact number/word this module looks
    for ("fails when more than **0.1% of its pixels**") — found live: the
    bold markers sat directly between an operator phrase and its number,
    breaking the adjacency match entirely and silently dropping the rule.
    Stripped once, up front, rather than woven into every regex."""
    return text.replace("**", "").replace("__", "")


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
    text = _strip_markdown_emphasis(text)
    rules: List[ThresholdRule] = []
    last_number: Optional[float] = None

    for sentence in _sentences(text):
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


def _claim_states_a_threshold(claim_text: str) -> bool:
    """True when the claim's own number sits directly in a comparison-
    operator phrase ("more than 0.1%") rather than being a bare instance
    value the claim reports an outcome for. Same adjacency shape
    `_extract_threshold_rules` looks for in passage text - finding it here
    means the claim is restating a threshold rule (e.g. "it fails when more
    than 0.1% differ"), not reporting a specific measurement, and this
    module has nothing to compare that restatement against - see issue #6,
    where treating the claim's own threshold number as an instance value
    produced a false `contradicts` on a claim that verbatim-restated the
    passage's own rule."""
    for op_pattern, _ in _COMPARISONS:
        if re.search(op_pattern + r"\s*" + _NUMBER_RE.pattern, claim_text, re.IGNORECASE):
            return True
    return False


def _extract_claim_value_outcome(claim_text: str) -> Optional[Tuple[float, str]]:
    """Exactly one number and exactly one outcome word in the claim, or
    None. Two-or-more numbers (likely compound - see issue #3), zero/two+
    recognized outcome words, or the claim's number itself being a
    threshold phrase rather than an instance value (see issue #6) all
    abstain rather than guess."""
    claim_text = _strip_markdown_emphasis(claim_text)
    numbers = _NUMBER_RE.findall(claim_text)
    if len(numbers) != 1:
        return None
    outcome_words = _outcome_words_in(claim_text)
    if len(outcome_words) != 1:
        return None
    if _claim_states_a_threshold(claim_text):
        return None
    return _parse_number(numbers[0]), _outcome_of(outcome_words[0])


def check_numeric_consistency(claim_text: str, passages: List[str]) -> Optional[NumericOverride]:
    """The deterministic answer for a numeric claim, or None to abstain and
    leave whatever verdict the judge already reached alone. Tries shape 1
    (module docstring) first, then shape 2, then shape 3 if neither has
    anything to say. The three are mutually exclusive by construction:
    shape 1 requires exactly one number in the claim; shapes 2 and 3 both
    require exactly two (one bound, one range) but look for entirely
    different phrase shapes ("at least X" vs "between X and Y"), so a
    claim matching one never also matches the other. Trying all three in
    sequence never double-fires.
    """
    result = _check_value_outcome_shape(claim_text, passages)
    if result is not None:
        return result
    result = _check_bound_restatement_shape(claim_text, passages)
    if result is not None:
        return result
    return _check_range_restatement_shape(claim_text, passages)


def _check_value_outcome_shape(claim_text: str, passages: List[str]) -> Optional[NumericOverride]:
    """Shape 1: fires only when the claim names exactly one value+outcome
    and the passages contain exactly one threshold rule whose region
    contains that value - anything else (no rule, multiple candidate
    rules, an unrecognized comparison phrase) is left to the judge,
    deliberately.
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

    # Retrieved passages routinely overlap at chunk boundaries (the same
    # sentence can come back twice, once at the tail of one chunk and again
    # at the head of the next) - collapse rules that are the literal same
    # fact before checking for ambiguity, so two passages AGREEING isn't
    # mistaken for two passages CONFLICTING.
    covering = list({(r.comparator, r.value, r.outcome): r
                      for r in rules if r.covers(value)}.values())
    if len(covering) != 1:
        return None  # no coverage, or genuinely conflicting rules - abstain

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


# Shape 2 (see issue #15, module docstring): a claim restating one of the
# document's own comparator+number bounds, with no outcome word on either
# side. Matching "the same fact" here can't rely on an outcome word the way
# shape 1 does - the signal instead is that most of the claim's own wording
# turns up inside the passage sentence once both numbers are stripped out.
# A containment coefficient (shared tokens / the SMALLER side's token
# count), not symmetric Jaccard, is what makes this hold up in practice: a
# retrieved passage sentence routinely carries a trailing clause the short
# claim never restates ("...to the target service, so a large document
# does not look like an attack") - Jaccard penalizes that extra tail as
# lost overlap, which pushed a real live match below a 0.6 threshold during
# testing; a containment coefficient scores it 1.0 as long as the claim's
# own words are all there, which is the actual question this needs
# answered.
_BOUND_MATCH_THRESHOLD = 0.75
_BOUND_TOKEN_RE = re.compile(r"[a-z0-9<>]+")


def _bound_statements(text: str) -> List[Tuple[str, float, str, str]]:
    """(comparator, value, sentence, stripped_template) for every sentence
    in `text` that states a comparator+number bound and has NO recognized
    outcome word - the complement of what `_extract_threshold_rules` looks
    for, so a sentence already claimed by shape 1 is never also a shape-2
    candidate."""
    text = _strip_markdown_emphasis(text)
    out: List[Tuple[str, float, str, str]] = []
    for sentence in _sentences(text):
        if _outcome_words_in(sentence):
            continue
        for op_pattern, comparator in _COMPARISONS:
            m = re.search(op_pattern + r"\s*" + _NUMBER_RE.pattern, sentence, re.IGNORECASE)
            if m:
                template = _NUMBER_RE.sub("<NUM>", sentence, count=1).lower()
                out.append((comparator, _parse_number(m.group(1)), sentence.strip(), template))
                break
    return out


def _bound_containment(claim_template: str, passage_template: str) -> float:
    """Fraction of the SHORTER template's tokens found in the other one -
    see the module-level comment above for why this, not Jaccard."""
    claim_tokens = set(_BOUND_TOKEN_RE.findall(claim_template))
    passage_tokens = set(_BOUND_TOKEN_RE.findall(passage_template))
    smaller = min(len(claim_tokens), len(passage_tokens))
    if smaller == 0:
        return 0.0
    return len(claim_tokens & passage_tokens) / smaller


def _check_bound_restatement_shape(claim_text: str, passages: List[str]) -> Optional[NumericOverride]:
    """Shape 2: fires only when the claim states exactly one comparator+
    number bound (no outcome word) and the passages contain exactly one
    bound, with the same comparator, that reads as the same fact -
    anything else (no bound in the claim, no match, a comparator mismatch,
    or genuinely conflicting matches) is left to the judge, deliberately.
    """
    claim_bounds = _bound_statements(claim_text)
    if len(claim_bounds) != 1:
        return None
    claim_comparator, claim_value, _, claim_template = claim_bounds[0]

    matches: List[ThresholdRule] = []
    for passage in passages:
        for comparator, value, sentence, template in _bound_statements(passage):
            if comparator != claim_comparator:
                continue
            if _bound_containment(claim_template, template) >= _BOUND_MATCH_THRESHOLD:
                matches.append(ThresholdRule(
                    comparator=comparator, value=value, outcome="", source_text=sentence,
                ))

    # Same chunk-boundary-overlap collapsing as shape 1: two passages
    # restating the identical bound isn't two conflicting matches.
    unique = list({(r.comparator, r.value): r for r in matches}.values())
    if len(unique) != 1:
        return None  # no match, or genuinely conflicting matches - abstain

    rule = unique[0]
    consistent = claim_value == rule.value
    verdict = VERDICT_ENTAILS if consistent else VERDICT_CONTRADICTS
    relation = "matches" if consistent else "does not match"
    explanation = (
        f"Deterministic bound check: the passages state '{rule.source_text}' "
        f"({rule.comparator} {rule.value:g}); the claim's own bound "
        f"({claim_comparator} {claim_value:g}) {relation} it."
    )
    return NumericOverride(verdict=verdict, explanation=explanation)


# Shape 3 (see issue #8): a claim restating a two-number range, matched
# against a passage stating a range for what reads as the same fact. Only
# two explicit phrasings are recognized — see the module docstring for why
# a bare "X-Y" form deliberately isn't a third.
_RANGE_BETWEEN_RE = re.compile(
    r"between\s+" + _NUMBER_RE.pattern + r"[a-zA-Z]*\s+and\s+" + _NUMBER_RE.pattern,
    re.IGNORECASE,
)
_RANGE_FROMTO_RE = re.compile(
    r"from\s+" + _NUMBER_RE.pattern + r"[a-zA-Z]*\s+to\s+" + _NUMBER_RE.pattern,
    re.IGNORECASE,
)
_RANGE_PATTERNS = [_RANGE_BETWEEN_RE, _RANGE_FROMTO_RE]
# Found live, not assumed from shape 2's own number: "A response between
# 200ms and 500ms is acceptable" against the document's actual wording
# ("Response times between 200ms and 500ms are considered acceptable
# under normal load") scores 0.6 -- a realistic paraphrase gap, not a
# contrived one -- which shape 2's inherited 0.75 threshold rejected
# outright. Lowered with headroom above that real case, while a genuinely
# unrelated range (a different subject entirely) still scores 0.0 on the
# same test document, since the unit suffix folded into the <RANGE>
# placeholder ("500ms" vs "500 attempts") breaks the match before word
# overlap is even considered.
_RANGE_MATCH_THRESHOLD = 0.5


def _range_statements(text: str) -> List[Tuple[float, float, str, str]]:
    """(low, high, sentence, stripped_template) for every sentence in
    `text` matching one of the two recognized range phrases — the numbers
    are sorted into (low, high) regardless of the order they're written
    in, since "between 500 and 200" and "between 200 and 500" state the
    same range."""
    text = _strip_markdown_emphasis(text)
    out: List[Tuple[float, float, str, str]] = []
    for sentence in _sentences(text):
        for pattern in _RANGE_PATTERNS:
            m = pattern.search(sentence)
            if m:
                a, b = _parse_number(m.group(1)), _parse_number(m.group(2))
                low, high = (a, b) if a <= b else (b, a)
                template = pattern.sub("<RANGE>", sentence, count=1).lower()
                out.append((low, high, sentence.strip(), template))
                break  # one range phrase claimed this sentence
    return out


def _claim_range(claim_text: str) -> Optional[Tuple[float, float, str]]:
    """Exactly one recognized range phrase and exactly two numbers total in
    the claim, or None — a third number anywhere else in the claim
    abstains, the same rule every other shape in this module follows."""
    claim_text = _strip_markdown_emphasis(claim_text)
    if len(_NUMBER_RE.findall(claim_text)) != 2:
        return None
    ranges = _range_statements(claim_text)
    if len(ranges) != 1:
        return None
    low, high, _, template = ranges[0]
    return low, high, template


def _check_range_restatement_shape(claim_text: str, passages: List[str]) -> Optional[NumericOverride]:
    """Shape 3: fires only when the claim states exactly one recognized
    range and the passages contain exactly one range, about what reads as
    the same fact (same containment-coefficient matching shape 2 uses),
    with an EXACT bound match on both ends. Anything else — no range in
    the claim, no matching range in the passages, or a claim range that is
    merely a subset of a wider stated range — is left to the judge,
    deliberately (see the module docstring).
    """
    claim_range = _claim_range(claim_text)
    if claim_range is None:
        return None
    claim_low, claim_high, claim_template = claim_range

    matches: List[Tuple[float, float, str]] = []
    for passage in passages:
        for low, high, sentence, template in _range_statements(passage):
            if _bound_containment(claim_template, template) >= _RANGE_MATCH_THRESHOLD:
                matches.append((low, high, sentence))

    # Same chunk-boundary-overlap collapsing shapes 1 and 2 already do.
    unique = list({(low, high): (low, high, sentence) for low, high, sentence in matches}.values())
    if len(unique) != 1:
        return None  # no match, or genuinely conflicting matches — abstain

    p_low, p_high, source_text = unique[0]
    consistent = claim_low == p_low and claim_high == p_high
    # A claim range strictly inside the passage's stated range is a true,
    # compatible narrower statement, not a restatement — and not a
    # contradiction either (see module docstring). Only a claim range that
    # extends outside what the passage states is flagged; a pure subset
    # abstains rather than being scored either way.
    if not consistent and claim_low >= p_low and claim_high <= p_high:
        return None
    verdict = VERDICT_ENTAILS if consistent else VERDICT_CONTRADICTS
    relation = "matches" if consistent else "does not match"
    explanation = (
        f"Deterministic range check: the passages state '{source_text}' "
        f"({p_low:g}-{p_high:g}); the claim's own range "
        f"({claim_low:g}-{claim_high:g}) {relation} it."
    )
    return NumericOverride(verdict=verdict, explanation=explanation)
