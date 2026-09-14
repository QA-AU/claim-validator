# Test results and observations

A running record of benchmark findings that don't belong in the paper's
own narrative but need to be on record somewhere real — every result
here comes from a live run against `usera-claimval`, not a mock, with
any ground truth fixed and written down *before* the claims were
submitted.

---

## 2026-09-14 — Volatility test, judge-model matrix, and adversarial taxonomy set

**Why:** a prior review of the working paper asked two specific,
answerable questions the paper itself hadn't measured: which verdict
boundary is actually fragile under repeated runs, and whether the
deployed default (Haiku) trades away accuracy other tiers wouldn't. A
second review then proposed a 13-category adversarial claim taxonomy to
find out whether the judge catches failure modes — quantifier shifts,
multi-hop claims, modal flattening — that plain factual inversions never
exercise.

**Corpus:** the two OpenSpec-adapter example documents
(`tools/openspec-adapter/examples/api-testing`,
`.../ui-testing`, 33 claims each, ground truth from their own README
seed tables) for the volatility and judge-model tests. The taxonomy set
adds one already-known document (the `api-testing` brief) and one
genuinely new, held-out domain — a synthetic corporate travel & expense
policy, hand-written for this test and not part of the repo's own
fixtures — 13 and 15 claims respectively, each hand-authored against a
category and a target verdict fixed before submission.

### Volatility (5 independent full resubmissions, 66 claims)

7.6% of claims (5/66) landed on a non-unanimous final verdict across 5
resubmissions of the identical claim set — lower than the judge's own
documented ~20–25% single-pass variance, as expected since this measures
the majority-of-3 outcome, not raw per-call noise.

| Transition | Count |
|---|---|
| `mentions_only ↔ no_evidence` | 3 |
| `contradicts ↔ mentions_only` | 1 |
| `entails ↔ mentions_only` | 1 |

**Finding:** `mentions_only` is the volatile hub — every fragile claim
touched it. Not one sharp seam; a soft category that borders all three
others.

### Judge-model matrix (Haiku / Sonnet / Opus, same claims, same retrieval)

| | api-testing | ui-testing |
|---|---|---|
| Haiku (deployed default) | 24/33 (72.7%) | 29/33 (87.9%) |
| Sonnet | 22/33 (66.7%) | 31/33 (93.9%) |
| Opus | 26/33 (78.8%) | 31/33 (93.9%) |

*(Corrected mid-run: the test harness's own ground truth had a bug —
`contract-verifier.R7.S1.A2` restates a retry-failure rule in an
attempt-count-agnostic way that's true regardless of the exact number, a
"consistent half" like two already-known `ui-testing` cases. All three
tiers correctly said `entails`; the first-pass scoring wrongly flagged
it as a miss for all three. Table above is corrected.)*

**Finding:** Haiku is not obviously worse — it beat Sonnet on both
documents. Opus was best on both, but by a modest margin (6 points on
each). Every tier's misses cluster on the same shape: `expected=entails,
got=mentions_only`, model-independent, most likely a retrieval/phrasing
gap between OpenSpec-generated requirement text and the source brief's
own wording, not a per-model judgment quality difference.

### Adversarial taxonomy set (28 claims, 2 documents)

Overall: **24/28 correct (85.7%)** — expense policy (new document):
14/15 (93.3%); api-testing (already-known document): 10/13 (76.9%).
Full claim text, answer keys, and raw per-claim results are preserved
in this session's scratchpad (`taxonomy/expense-claims.json`,
`taxonomy/expense-answer-key.json`, `taxonomy/apitest-claims.json`,
`taxonomy/apitest-answer-key.json`, `taxonomy_result_*.json`) — not
committed to the repo, since they were a one-off benchmark run rather
than a permanent fixture; worth promoting into the repo proper if this
taxonomy gets reused or scaled up.

| Category | Result |
|---|---|
| Plain factual match / inversion | 4/4 |
| Modal flattening | 1/1 |
| Negation in a compound clause | 2/2 |
| Entity substitution | 2/2 |
| Compound claim, mixed truth | 2/2 |
| Paraphrase only, no lexical overlap | 2/2 |
| On-topic but unstated specific | 2/2 |
| Plausible fabrication | 1/2 |
| Superseded/versioned clause | 1/1 |
| **Multi-hop** | **4/4** |
| **Quantifier shift** | **0/2** |
| Numeric boundary | 1/2 |

**Finding — multi-hop claims are caught correctly, 4/4.** Every claim
true only by combining two separate, non-adjacent passages resolved
correctly (3/3 or 2/3 agreement) on both documents. Very likely the
compound-claim retrieval widening fix (issue #3) doing exactly the job
it was built for — these claims are syntactically compound in the same
shape that fix targets.

**Finding — quantifier-shift weakening is not caught, 0/2, both at full
3/3 agreement.** Two claims that weakened a document's "every"/"all"
into "some"/"most" both came back `entails`. The judge's own reasoning
in both cases applies valid strict logic ("every request requires
approval, which makes it true that some requests require approval") —
technically not false, but it misses the real-world implicature a human
reader would flag: stating "some" when the true rule is "all, no
exceptions" is misleading in practice even though not strictly
contradicted by it. Reproduced independently on two unrelated documents,
not a one-off. Filed as [#14](https://github.com/QA-AU/claim-validator/issues/14).

**Finding — a numeric off-by-one on a rate limit, with the judge's own
reasoning contradicting its own verdict.** A claim loosening "no more
than 10 requests per second" to "no more than 11" came back `entails`
at only 2/3 agreement, while the judge's own stated reasoning correctly
computed "11 is above the threshold of 10, so it exceeds the limit" —
the reasoning argues for `contradicts`; the verdict says `entails`. A
different numeric shape than the existing threshold checker (issues #4,
#6) already covers — a rate ceiling being loosened, not a restated
document rule — so it isn't caught by that fix. Filed as
[#15](https://github.com/QA-AU/claim-validator/issues/15).

**Finding — `mentions_only`/`no_evidence` miscalibration, corroborated
on a fresh, independently-labeled claim.** A fabricated claim the
document is entirely silent on came back `mentions_only`, while the
judge's own reasoning correctly stated the passages "are silent on"
it — textbook `no_evidence`. This is the same boundary the volatility
test above already flagged as the fragile hub, now showing up as a
genuine miscalibration on a claim with fixed, independently-authored
ground truth, not just as run-to-run noise. Not filed as a new issue —
it corroborates an already-documented limitation rather than
identifying a new one.
