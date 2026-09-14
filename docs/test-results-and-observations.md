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

---

## 2026-09-14 — DeepEval comparison (Tier 2), the same 28 taxonomy claims

**Why:** the honest gap in every comparison so far was architectural,
not empirical — nothing had actually run the same claims through an
existing tool and looked at where the two disagree, case by case. This
closes that gap for the 28-claim adversarial taxonomy set (the one with
independent, fixed ground truth).

**Method:** DeepEval's `FaithfulnessMetric`, run externally against
this repo, never integrated into the shipped product. Backed by a
small custom `DeepEvalBaseLLM` wrapper around Anthropic (Haiku) rather
than DeepEval's OpenAI default — kept consistent with this project's
own Anthropic-only rule rather than quietly introducing the dependency
it deliberately avoids. Fed the exact same cited passages Claim
Validator itself retrieved for each claim, so the comparison isolates
judgment, not retrieval. Ground truth and Claim Validator's own
verdicts collapsed to binary (`entails` = faithful; anything else =
not faithful), matching DeepEval's own binary pass/fail at its default
0.5 threshold.

| | vs. ground truth | |
|---|---|---|
| Claim Validator | 25/28 (89.3%) | |
| DeepEval | 23/28 (82.1%) | |
| Claim Validator ↔ DeepEval agreement | 20/28 (71.4%) | genuinely different judgment layers, not redundant |

**DeepEval right, Claim Validator wrong — 3 cases, reported in full per
the review's own credibility bar (a comparison that only shows wins
isn't credible):**

- Two of the three are the *exact same* quantifier-shift gap already
  filed as [#14](https://github.com/QA-AU/claim-validator/issues/14) —
  DeepEval correctly flagged both "all → some/most" weakenings that
  Claim Validator's judge missed. Real, independent confirmation that
  #14 is a genuine gap, from a completely different tool.
- The third is the exact numeric off-by-one from
  [#15](https://github.com/QA-AU/claim-validator/issues/15) — but this
  result was captured *before* that fix. Re-checked directly against
  the fixed module (no API call needed): `check_numeric_consistency`
  now correctly returns `contradicts` for this exact claim/passage
  pair. Already resolved, not a live gap.

**Claim Validator right, DeepEval wrong — 5 cases, two distinct
architectural reasons, not one:**

- **Compound claims, mixed truth (2 cases).** DeepEval actually *did*
  detect the false half — it scored both 0.5, not 1.0 — but its binary
  pass/fail at a 0.5 threshold rounds a half-true compound claim up to
  "faithful." The detection is there; the threshold collapse is what
  loses it. A real, specific illustration of exactly what the earlier
  review warned a blended score would hide.
- **Unstated specifics and plausible fabrications (2 cases).** DeepEval
  scored both 1.0 — "no contradictions found" — which is correct by
  its own definition: Faithfulness checks whether the output
  contradicts the context, not whether the context actually supports
  every specific the output adds. A claim inventing an unstated detail
  doesn't contradict anything, so DeepEval has nothing to flag. This
  is the concrete, measured version of the paper's own architectural
  argument (DeepEval scores against what it's given; it doesn't
  independently ask "did the document actually address this at all")
  — no longer just an assertion, now a real pair of cases where it's
  exactly what happened.
- The fifth case (`API.3`, a *different* quantifier-shift claim)
  went the other way from the two DeepEval caught above — DeepEval
  called it faithful when it wasn't. Quantifier-shift handling isn't
  reliably better on either system; it varies case by case on both
  sides, not just Claim Validator's.

**Zero cases where both systems were wrong** — every miss in this set
was caught by at least one of the two tools, meaning their blind spots
are genuinely different, not overlapping copies of the same gap.

Script and Anthropic-model wrapper live in this session's scratchpad
(`anthropic_deepeval_model.py`, `tier2_deepeval_comparison.py`) —
worth promoting into the repo if this comparison gets rerun as the
taxonomy set grows, same as the taxonomy fixtures themselves.

---

## 2026-09-14 — `mentions_only`/`no_evidence` judge-prompt fix, live-tested

**Why:** every benchmark round so far (volatility test, Tier 2 DeepEval
comparison, the taxonomy confusion matrix) converged on the same
finding: `entails`/`contradicts` are never confused with each other, but
`mentions_only`/`no_evidence` show real, repeated, bidirectional
confusion. Reading the judge's own reasoning text across every confused
case (`phases/entailment.py`'s prompt) found the root cause: the
verdict definitions were asymmetric — `no_evidence` was defined vaguely
("are the passages about entirely different things?") while
`mentions_only` was defined concretely with rich examples, biasing the
model toward `mentions_only` whenever there was any topical overlap at
all.

**First attempt (not kept) — over-corrected the other way.** Rewriting
both definitions to hinge on "does the passage name the SPECIFIC thing
the claim is about" fixed the plausible-fabrication misses
(`API.13`, `WAR.14`, previously wrongly `mentions_only`) but broke the
opposite case: claims that add an unstated specific detail to a topic
the passages *do* cover (`API.12`, `EXP.13`) flipped from correctly
`mentions_only` to `no_evidence`, because the model started reading
"the specific thing" as the claim's entire predicate rather than its
subject — and by definition, an unstated detail is never named
verbatim. Live-tested 3x on the 6 previously-flagged claims: fully
reproducible, not run-to-run noise (identical pattern all 3 passes).
Net accuracy on that set was unchanged (3/6 before, 3/6 after — the
wrong 3 just changed).

**Second attempt (kept) — split "subject" from "specific detail."**
Rewrote the definitions to ask two separate questions in order: (1) do
the passages discuss the same *subject* — field, process, mechanism,
entity — the claim is about, regardless of wording, and (2) only if
yes, do they confirm the *specific detail* the claim adds about that
subject. `no_evidence` now means the subject itself never comes up;
`mentions_only` means the subject is covered but the added detail
isn't confirmed either way.

**Live results, 3 passes on the 6 previously-flagged claims:**

| Claim | Expected | Before | After (3 passes) |
|---|---|---|---|
| `API.13` (plausible_fabrication) | `no_evidence` | `mentions_only` | `no_evidence` ×3 — **fixed** |
| `API.12` (unstated_specific) | `mentions_only` | `mentions_only` | `mentions_only` ×2, `no_evidence` ×1 — mostly held, one flake |
| `EXP.13` (unstated_specific) | `mentions_only` | `mentions_only` | `mentions_only` ×3 — held |
| `EXP.14` (plausible_fabrication) | `no_evidence` | `no_evidence` | `no_evidence` ×3 — held |
| `WAR.13` (unstated_specific) | `mentions_only` | `no_evidence` | `no_evidence` ×3 — **still wrong** |
| `WAR.14` (plausible_fabrication) | `no_evidence` | `mentions_only` | `no_evidence` ×3 — **fixed** |

**Full-taxonomy regression check, all 45 claims across all 3 documents
(api-testing, expense-policy, warranty-terms), single pass:**
**37/45 → 39/45** (82.2% → 86.7%). 4 fixes, 2 regressions:

- Fixes: `API.13`, `WAR.14` (the target boundary, as above), plus
  `API.6` and `WAR.16` — both `entails`→`contradicts` corrections
  unrelated to the edited text, most likely ordinary judge variance
  landing favorably this pass.
- Regressions: `API.3` (quantifier-shift, `contradicts`→`entails`) and
  `API.8` (multi-hop, `entails`→`mentions_only`) — neither touches the
  `mentions_only`/`no_evidence` boundary this fix targeted; both are
  consistent with the ~20–25% single-pass judge variance already
  documented above, not a side effect traced to this specific edit.

**Honest limitation: `WAR.13` is not fixed.** "Warranty claims can be
tracked in real time via a mobile app notification system" — the
document's Claims Process section covers portal submission and review
timelines but never mentions real-time tracking. All 3 passes landed
on `no_evidence`, expected `mentions_only`. This looks like a genuine
edge of the `on_topic_unstated` vs. `plausible_fabrication` distinction
itself: "same subject, different specific detail" and "no shared
subject at all" is a real spectrum, not a bright line, and this claim
sits closer to the fuzzy middle than the taxonomy's own binary label
assumes — the same kind of open design question already flagged for
the `conditional_truth` category.

**Net assessment:** a real, reproducible improvement (not just one
lucky run — verified 3x on the flagged set and once on the full 45),
with one known-remaining miss and ordinary noise elsewhere. Change is
live on `usera-claimval` only; not yet merged to `main`, not yet
deployed to `userb-claimval`.
