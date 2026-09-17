"""A deterministic, non-LLM safety net for one specific judge failure mode
on claims about *code*: see issue #17's own live case, `docs/refactor-
accuracy-demo/`.

The judge is textual entailment — it compares a claim's wording to a
passage's wording. That works well for claims about facts, and fails on a
narrow but real class of claims about a *property* of code (thread safety,
atomicity, race-freedom) whenever the code carries a comment or identifier
asserting the same property. #17 added prompt guidance against this
directly; this module is the deterministic backstop, the same relationship
`numeric_threshold_check.py` has to `_THRESHOLD_PROCEDURE` — a prompt fix
narrows how often the mistake happens, code that doesn't reason in
free text can't make it at all.

Deliberately narrow, matching this checker's own history (issues #4, #6,
#8, #15 each added one numeric shape at a time, not a general arithmetic
engine): this fires only on a claim asserting concurrency-safety of a
data update, nothing else. A claim about security, idempotency, or any
other property is left to the judge — extending this to other properties
is a natural later increment once this one is proven live, not assumed
here.

Fires when the CLAIM contains a concurrency-safety trigger phrase
("prevents race conditions", "thread-safe", ...) and NONE of the cited
passages contain a real synchronization marker (a transaction, a lock, a
conditional guard on the value being written). The absence of a marker is
not proof the code is unsafe — plenty of genuinely safe code uses a
mechanism this module doesn't recognize — so the override always lands on
`mentions_only`, never `contradicts`: the update is there, the safety
property claimed about it is not confirmed.
"""

import re
from dataclasses import dataclass
from typing import List, Optional

VERDICT_MENTIONS_ONLY = "mentions_only"

# Case-insensitive. Matched as substrings, not whole-word — "atomically"
# should still fire the same as "atomic".
_TRIGGER_PHRASES = (
    "prevent race condition",
    "prevents race condition",
    "prevent a race condition",
    "prevents a race condition",
    "race condition safe",
    "race-safe",
    "thread-safe",
    "thread safe",
    "atomically",
    "atomic update",
    "atomic batch",
)

# Real synchronization/guard vocabulary. Kept deliberately generic across
# SQL and general-purpose code rather than one language's exact syntax —
# missing a marker only means this module abstains (returns None), it
# never claims the absence of a marker as proof of anything beyond "not
# confirmed", so a false negative here just leaves the judge's own verdict
# standing, same as any other abstention in this module.
_SYNC_MARKERS = (
    "transaction",
    "begin;",
    "commit;",
    "for update",
    "mutex",
    "synchronized",
    "compare-and-swap",
    "compare and swap",
    "optimistic lock",
    "row lock",
    "select ... for update",
)

# A WHERE clause guarded by a comparison against the value being written —
# `UPDATE ... SET stock = stock - $n WHERE stock >= $n` — the one common
# atomic-decrement shape not covered by the fixed marker list above,
# since it's a pattern, not a keyword.
_GUARDED_WHERE_RE = re.compile(r"where\s+[^;]{0,80}?(>=|<=|>|<)", re.IGNORECASE)


@dataclass
class ConcurrencyOverride:
    verdict: str
    explanation: str


def _claim_has_trigger(claim_text: str) -> bool:
    lowered = claim_text.lower()
    return any(phrase in lowered for phrase in _TRIGGER_PHRASES)


def _passages_show_a_sync_mechanism(passages: List[str]) -> bool:
    for passage in passages:
        lowered = passage.lower()
        if any(marker in lowered for marker in _SYNC_MARKERS):
            return True
        if _GUARDED_WHERE_RE.search(lowered):
            return True
    return False


def check_concurrency_consistency(
    claim_text: str, passages: List[str]
) -> Optional[ConcurrencyOverride]:
    """The deterministic answer for a concurrency-safety claim, or None to
    abstain and leave whatever verdict the judge already reached alone.

    Only ever called on a claim the judge has already scored `entails` —
    see `pipeline.py::_build_claim_result`. This module only ever *removes*
    unearned confidence, never grants it: it has no shape that upgrades a
    claim to `entails`, only one that downgrades an unconfirmed one to
    `mentions_only`.
    """
    if not _claim_has_trigger(claim_text):
        return None
    if _passages_show_a_sync_mechanism(passages):
        return None
    return ConcurrencyOverride(
        verdict=VERDICT_MENTIONS_ONLY,
        explanation=(
            "Deterministic concurrency check: the claim asserts a "
            "concurrency-safety property (race prevention, atomicity, or "
            "thread-safety), but none of the cited passages show a "
            "recognized synchronization mechanism (a transaction, a lock, "
            "or a guarded conditional update) tied to the value being "
            "written. Combining several writes into one statement, or a "
            "comment asserting the same property, is not itself evidence "
            "the update is safe under concurrency."
        ),
    )
