"""An alternative to `phases/entailment.py`'s majority-vote judge: one call
per claim, reading the model's own confidence from token probabilities
instead of from agreement across repeated samples.

Optional, and gated on capability rather than provider name — `pipeline.py`
calls this only when `hasattr(llm_client, "generate_with_logprobs")` is true
(currently only `OllamaClient`; Anthropic's Messages API does not expose
token probabilities). Any client without that method keeps using the
existing majority-vote path unchanged.

### Why this exists alongside majority voting, not instead of it

Majority voting over `runs` samples can only ever report one of `runs + 1`
discrete confidence buckets — 0/3, 1/3, 2/3, 3/3. Two claims can both come
back "3/3 entails" while one was a landslide on every run and the other was
each run barely leaning that way; the vote cannot tell them apart, only the
words agreed. Reading the actual token probability behind a single answer is
continuous, not bucketed, and costs one call instead of three per claim.

### Why one claim per call, not `phases/entailment.py`'s batch of three

Logprobs are read at a specific token position in the response. Batching
several claims into one JSON-array reply (as the majority-vote judge does)
would require locating which token(s) correspond to which claim's verdict
word inside that one response — a real alignment problem, since a word like
"MENTIONS_ONLY" is not guaranteed to be a single token for every tokenizer.
One claim, one short answer, sidesteps that: the first output token alone is
enough to identify the verdict, because the four candidate words all start
with a different letter (Contradicts / No_evidence / Mentions_only /
Entails) by deliberate choice below.
"""

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from phases.entailment import (
    VERDICT_CONTRADICTS,
    VERDICT_ENTAILS,
    VERDICT_MENTIONS_ONLY,
    VERDICT_NO_EVIDENCE,
    VERDICTS,
    EntailmentReport,
    EntailmentVerdict,
)

logger = logging.getLogger(__name__)


class LogprobsUnsupportedError(RuntimeError):
    """The client has `generate_with_logprobs` (so the capability check in
    pipeline.py let it through) but a real call returned no token data.

    Found live: Ollama's cloud-hosted models (e.g. gpt-oss:120b-cloud)
    answer the request but never populate the `logprobs` field at all —
    only genuinely local models do. That is a property of the specific
    model/backend, not of the client class, so `hasattr` cannot see it in
    advance; it only shows up on a real call. Raised after the first call so
    a caller in "auto" mode can retry the whole judge with the majority-vote
    path instead of quietly reporting "confidence unavailable" on every
    single claim for the rest of the run.
    """

JUDGE_CHUNK_CHARS = 600

# The word each verdict is spelled as in the prompt and the reply. Ordered so
# every one starts with a different letter — the property the single-token
# read below depends on.
_VERDICT_WORD = {
    VERDICT_CONTRADICTS: "CONTRADICTS",
    VERDICT_NO_EVIDENCE: "NO_EVIDENCE",
    VERDICT_MENTIONS_ONLY: "MENTIONS_ONLY",
    VERDICT_ENTAILS: "ENTAILS",
}
assert len({w[0] for w in _VERDICT_WORD.values()}) == len(_VERDICT_WORD), (
    "the single-token confidence read requires four distinct first letters"
)


def _claim_of(requirement) -> str:
    parts = [requirement.title]
    if requirement.expected_behavior:
        parts.append(f"Expected: {requirement.expected_behavior}")
    if requirement.criteria:
        parts.append("Criteria: " + "; ".join(requirement.criteria))
    return " | ".join(p for p in parts if p)


def _build_prompt(requirement, passages: List[Tuple[int, str]]) -> str:
    cited = "\n".join(
        f"    [chunk {n}] {text[:JUDGE_CHUNK_CHARS]!r}" for n, text in passages
    )
    words = ", ".join(_VERDICT_WORD.values())
    return f"""Decide whether this claim is supported by the passages it cites.

claim: {_claim_of(requirement)}
cited_passages:
{cited}

Work through these tests IN ORDER and stop at the first that applies:

1. CONTRADICTS   — do the passages specify something INCOMPATIBLE with the
                    claim for the same case?
2. NO_EVIDENCE   — are the passages about entirely different things?
3. MENTIONS_ONLY — the passages concern the same subject and are simply
                    SILENT on what the claim asserts.
4. ENTAILS       — the passages state the claim, or it follows directly.

Reply with exactly one word and nothing else — no punctuation, no
explanation: {words}"""


def _extract_confidence(
    tokens: List[Dict[str, Any]],
) -> Tuple[Optional[str], Dict[str, float], Optional[str]]:
    """The verdict and per-candidate confidence from the first output token.

    Returns (verdict, probabilities_by_verdict, raw_first_token). Probability
    mass is normalized across just the four candidate words — "how sure was
    the model, conditioned on it being one of these four" — since the raw
    top_logprobs are drawn from the full vocabulary and most of that mass is
    words that were never going to be the answer.

    A candidate absent from `top_logprobs` (Ollama only returns the top N)
    is not zero — it is unknown, and is left out of the returned mapping
    rather than assigned an invented probability. The generated token itself
    is always included even if it fell outside `top_logprobs`, since it is
    known exactly: it is what the model actually produced.
    """
    if not tokens:
        return None, {}, None

    first = tokens[0]
    generated_text = str(first.get("token", "")).strip().upper()

    candidates: Dict[str, float] = {}

    def _matched_verdict(token_text: str) -> Optional[str]:
        normalized = token_text.strip().upper()
        if not normalized:
            return None
        for verdict, word in _VERDICT_WORD.items():
            if word.startswith(normalized) or normalized.startswith(word[:1]):
                return verdict
        return None

    # The token actually generated — known exactly, in or out of top_logprobs.
    generated_verdict = _matched_verdict(generated_text)
    if generated_verdict is not None:
        candidates[generated_verdict] = math.exp(float(first.get("logprob", 0.0)))

    for alt in first.get("top_logprobs") or []:
        verdict = _matched_verdict(str(alt.get("token", "")))
        if verdict is not None and verdict not in candidates:
            candidates[verdict] = math.exp(float(alt.get("logprob", 0.0)))

    if not candidates:
        return None, {}, generated_text

    total = sum(candidates.values())
    normalized = (
        {v: round(p / total, 4) for v, p in candidates.items()} if total > 0 else candidates
    )
    best_verdict = max(normalized, key=normalized.get)
    return best_verdict, normalized, generated_text


@dataclass
class _Judgeable:
    requirement: Any
    passages: List[Tuple[int, str]]


def judge_entailment_logprob(
    requirements,
    chunks: List[str],
    llm_client,
    top_logprobs: int = 5,
) -> EntailmentReport:
    """One call per claim, confidence read from token probabilities.

    Same `EntailmentReport`/`EntailmentVerdict` shapes `judge_entailment`
    returns, so nothing downstream (pipeline.py, report_excel.py) needs to
    know which judge actually ran — only `verdict.method`/`.confidence`
    distinguish them, for a reader who wants to know which kind of number
    they are looking at.
    """
    report = EntailmentReport(requirements_total=len(requirements))

    judgeable: List[_Judgeable] = []
    for requirement in requirements:
        passages = [
            (n, chunks[n])
            for n in (requirement.source_chunks or [])
            if isinstance(n, int) and 0 <= n < len(chunks)
        ]
        if passages:
            judgeable.append(_Judgeable(requirement, passages))
        else:
            report.verdicts.append(
                EntailmentVerdict(requirement_id=requirement.id, judged=False)
            )

    checked_support = False
    for item in judgeable:
        prompt = _build_prompt(item.requirement, item.passages)
        try:
            result = llm_client.generate_with_logprobs(prompt, temperature=0.0,
                                                         top_logprobs=top_logprobs)
        except Exception as e:
            report.failed_batches += 1
            logger.error(
                f"[LogprobJudge] {item.requirement.id!r} failed: {e}"
            )
            continue

        verdict, probabilities, raw_token = _extract_confidence(result.tokens)

        # Checked once, on the first call that actually completed — not
        # "logprobs came back empty" alone, but "logprobs never resolved to
        # one of the four words at all". A cloud-hosted model fails this by
        # returning nothing; a thinking model fails it a different way, by
        # returning real-looking tokens that are its reasoning trace, not
        # its answer (found live: qwen3.8 returned exactly one token, "The"
        # — the start of a sentence, while the actual answer, "ENTAILS", was
        # in `response` but never appeared in `logprobs` at all). Either
        # failure means this call cannot back its own claimed confidence.
        if not checked_support:
            checked_support = True
            if verdict is None:
                raise LogprobsUnsupportedError(
                    f"{llm_client.__class__.__name__} did not produce a "
                    f"usable verdict from logprobs on the first call "
                    f"(raw first token: {raw_token!r}) — either the model "
                    f"returns no logprobs at all (a cloud-hosted model), or "
                    f"they don't correspond to its actual answer (a "
                    f"thinking/reasoning model, whose logprobs may reflect "
                    f"its hidden reasoning instead)"
                )

        if verdict is None:
            # Logprobs were unusable (Ollama returned none, or the first
            # token matched none of the four words) — fall back to reading
            # the plain response text, the same way the majority-vote judge
            # would, rather than losing this claim entirely.
            upper = (result.text or "").strip().upper()
            verdict = next(
                (v for v, w in _VERDICT_WORD.items() if upper.startswith(w)), None
            )
        if verdict is None or verdict not in VERDICTS:
            report.failed_batches += 1
            logger.warning(
                f"[LogprobJudge] {item.requirement.id!r}: unparseable reply "
                f"{result.text[:60]!r}"
            )
            continue

        report.verdicts.append(EntailmentVerdict(
            requirement_id=item.requirement.id,
            verdict=verdict,
            reason=f"logprob judge, {round(probabilities.get(verdict, 0.0) * 100)}% "
                   f"confidence" if probabilities else "logprob judge",
            cited_chunks=[n for n, _ in item.passages],
            judged=True,
            agreement=1,
            runs_judged=1,
            verdicts_seen={verdict: 1},
            method="logprob",
            confidence=probabilities.get(verdict),
        ))

    report.runs = 1
    return report
