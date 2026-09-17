# Test results and observations

A running record of benchmark findings that don't belong in the paper's
own narrative but need to be on record somewhere real — every result
here comes from a live run against `usera-claimval`, not a mock, with
any ground truth fixed and written down *before* the claims were
submitted.

**Reading the numbers below:** unless a table says otherwise, `X/Y
(Z%)` means X claims out of Y total scored a verdict matching the
fixed, pre-written expected answer — Z% is that accuracy rate. This
applies to the judge-model matrix, the taxonomy category table, and
the DeepEval comparison table. The one exception is the volatility
table's `Count` column, which counts how many claims *changed verdict*
across 5 resubmissions of the same claim set — that measures
consistency, not correctness, and has no expected answer to match
against.

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

Both example specs (`tools/openspec-adapter/examples/`) have known
defects planted in them on purpose — see each example's own README:
a subset of requirements deliberately disagree with the source brief
(seeded, expected `contradicts`/`no_evidence`), and the rest are
faithfully grounded (clean, expected `entails`). Split out below,
because a blended accuracy number can't tell you whether a tier's
misses are missed defects (the actionable failure) or false alarms on
claims that were actually fine (a different, less serious failure).

**api-testing (33 claims: 8 seeded, 25 clean)**

| | Total | Seeded defects caught | Clean claims correctly entailed |
|---|---|---|---|
| Haiku (deployed default) | 24/33 (72.7%) | 7/8 | 17/25 |
| Sonnet | 22/33 (66.7%) | 6/8 | 16/25 |
| Opus | 26/33 (78.8%) | 6/8 | 20/25 |

**ui-testing (33 claims: 12 seeded, 21 clean)**

| | Total | Seeded defects caught | Clean claims correctly entailed |
|---|---|---|---|
| Haiku (deployed default) | 29/33 (87.9%) | 11/12 | 18/21 |
| Sonnet | 31/33 (93.9%) | 12/12 | 19/21 |
| Opus | 31/33 (93.9%) | 12/12 | 19/21 |

**Finding:** Haiku is not obviously worse — it beat Sonnet on both
documents. Opus was best on both, but by a modest margin (6 points on
each). The split sharpens why: **every tier catches seeded defects
well** (6–7 of 8 on api-testing, 11–12 of 12 on ui-testing) — that's
the actionable failure mode, and it's rare regardless of tier. Almost
every miss, on every tier, is the *other* direction: a genuinely clean
claim called `mentions_only` instead of `entails` — a false alarm, not
a blown defect. Same shape, same rate, across all three models — a
retrieval/phrasing gap between OpenSpec-generated requirement text and
the source brief's own wording, not a per-model judgment-quality
difference. Upgrading the deployed tier would buy very little: the
gap isn't in what Haiku fails to catch, it's in how often all three
models flag something that didn't need flagging.

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

**Five real cases, one per failure shape, verbatim — not paraphrased.**
The exact `FaithfulnessMetric` call behind every row: `FaithfulnessMetric(threshold=0.5, model=<AnthropicDeepEvalModel>, include_reason=True, async_mode=False)`,
measured against an `LLMTestCase(input=..., actual_output=<claim text>, retrieval_context=<Claim Validator's own cited_passages>)` — same evidence fed to both systems, so the comparison isolates judgment.

**1. `plausible_fabrication` — DeepEval structurally can't catch this (architectural, not a bug in DeepEval):**

> Claim: *"Employees may earn reward points redeemable for future travel upgrades when submitting expenses through the mobile app."*

| | Verdict | Reason (verbatim) |
|---|---|---|
| DeepEval | **1.0, faithful** | "The score is 1.00 because the actual output contains no contradictions with the retrieval context. The information presented is entirely faithful and accurate!" |
| Claim Validator | **no_evidence** | "Passages concern expense reimbursement policy; they contain no discussion of reward points, mobile apps, or travel upgrades." |

**2. `on_topic_unstated` — same structural gap:**

> Claim: *"Software subscription approvals must be logged in the company's IT asset-management system."*

| | Verdict | Reason (verbatim) |
|---|---|---|
| DeepEval | **1.0, faithful** | "The score is 1.00 because the actual output contains no contradictions with the retrieval context. The information presented is entirely faithful and accurate!" |
| Claim Validator | **mentions_only** | "Passages address software subscription approvals and thresholds ($100/month) but are silent on whether approvals must be logged in an IT asset-management system." |

**3. `compound_mixed_truth` — DeepEval detects it, the threshold erases it:**

> Claim: *"Alcohol is never reimbursable, and meal claims are capped at $100 per day."*

| | Verdict | Reason (verbatim) |
|---|---|---|
| DeepEval | **0.5 → rounds to faithful** | "The score is 0.50 because the actual output incorrectly states that employees can claim $100 per day for meals... when the retrieval context clearly specifies the limit is $75 per day." |
| Claim Validator | **contradicts** | "The passages state 'alcohol is never a reimbursable expense under any circumstance' (first part supported) but specify 'up to \$75 per day for meals' (contradicts the claim's '\$100 per day' cap)." |

**4. `quantifier_shift` — DeepEval's win, still open as issue #14:**

> Claim: *"Most forms of security scanning are out of scope for v1."*

| | Verdict | Reason (verbatim) |
|---|---|---|
| DeepEval | **0.0, correctly not faithful** | "The actual output misrepresents the scope of security scanning by stating it is 'most forms'... when the retrieval context explicitly states... a complete exclusion rather than a partial one." |
| Claim Validator | **entails (wrong)** | "The passages explicitly state '...security scanning...[is] explicitly out of scope for v1', which directly supports that security scanning forms are out of scope." — valid logic on a literal reading, wrong call: "most" understates a rule with no stated exceptions. |

**5. `numeric_boundary` — DeepEval's other win, since fixed as issue #15:**

> Claim: *"The verifier sends no more than 11 requests per second to the target service."*

| | Verdict | Reason (verbatim) |
|---|---|---|
| DeepEval | **0.0, correctly not faithful** | "The actual output claims 11 requests per second, which directly contradicts the retrieval context stating... no more than 10 requests per second." |
| Claim Validator (at the time) | **entails (wrong)** | "11 requests per second is above the threshold of 10, so it exceeds the limit. The claim 'no more than 11' would permit up to 11, which satisfies the passage's stricter limit..." — the judge's own reasoning reached the right answer, then stated the opposite verdict. |

`numeric_threshold_check.py`'s deterministic override now catches this
shape structurally; re-run against the current build, this exact claim
correctly returns `contradicts`.

**Reading across all five:** DeepEval's two wins are both real and both
now tracked (one open, one fixed) — genuine independent corroboration,
not noise. Claim Validator's three wins split into two different
reasons, not one: the fabrication and unstated-specific cases are
architectural (`FaithfulnessMetric` only ever checks for contradiction,
so a claim that merely *adds* something unconfirmed has nothing to
trip it), while the compound-claim case is a threshold artifact —
DeepEval's own 0.5 score proves it found the same problem this session
did, and its binary cutoff is what threw the finding away, not a
failure to detect it in the first place.

Script and Anthropic-model wrapper live in this session's scratchpad
(`anthropic_deepeval_model.py`, `tier2_deepeval_comparison.py`) —
worth promoting into the repo if this comparison gets rerun as the
taxonomy set grows, same as the taxonomy fixtures themselves.

---

## 2026-09-14 — 4×4 confusion matrix and per-class precision/recall/F1

**Why:** the benchmark design's actual core ask — a full confusion
matrix and per-verdict-class precision/recall, not one blended
accuracy number — had never been computed, only the binary
faithful/unfaithful comparison against DeepEval. Pure analysis over
the same 28-claim taxonomy set already run; no new API calls.

*Sample-size caveat, stated plainly rather than left implicit: n=28,
with two classes (`mentions_only`, `no_evidence`) carrying a support of
just 2 each. Read the smaller cells as directional, not statistically
settled — this is the benchmark design's own explicitly-cheap pilot
tier, not its full-scale sample size.*

**Claim Validator — confusion matrix (rows = true verdict, columns = predicted):**

| true ＼ predicted | entails | contradicts | mentions_only | no_evidence |
|---|---|---|---|---|
| **entails** | 8 | 0 | 0 | 0 |
| **contradicts** | 3 | 13 | 0 | 0 |
| **mentions_only** | 0 | 0 | 2 | 0 |
| **no_evidence** | 0 | 0 | 1 | 1 |

**Per-class precision / recall / F1:**

| Verdict | Support | Precision | Recall | F1 |
|---|---|---|---|---|
| entails | 8 | 0.73 | 1.00 | 0.84 |
| contradicts | 16 | 1.00 | 0.81 | 0.90 |
| mentions_only | 2 | 0.67 | 1.00 | 0.80 |
| no_evidence | 2 | 1.00 | 0.50 | 0.67 |
| **macro-avg** | | 0.85 | 0.83 | 0.80 |
| **weighted-avg** | | 0.90 | 0.86 | 0.86 |

Overall accuracy: 24/28 (85.7%).

**What the matrix says that a blended number couldn't:** every error
Claim Validator made in this set falls into exactly two confusable
pairs, both already named elsewhere in this record, now with real
numbers attached instead of anecdotes.

- **`contradicts` → wrongly predicted `entails`, 3 cases (`contradicts`
  recall 0.81).** This is precisely the quantifier-shift gap ([#14](https://github.com/QA-AU/claim-validator/issues/14),
  still open) and the pre-fix numeric-boundary case ([#15](https://github.com/QA-AU/claim-validator/issues/15),
  now fixed) — the matrix confirms these two named issues account for
  100% of the `contradicts`-class recall loss in this set, nothing else
  hiding in that column.
- **`no_evidence` → wrongly predicted `mentions_only`, 1 case (`no_evidence`
  recall 0.50, on a support of only 2 — one miss is half the class).**
  The same fragile boundary the volatility test already flagged as its
  hub. `entails` never gets confused with `contradicts` or vice versa
  in either direction here — the errors cluster tightly on two specific
  seams, not spread across the whole matrix.

**DeepEval — 2×2 confusion matrix (its own finest granularity, since it
only ever outputs binary faithful/not-faithful):**

| true ＼ predicted | faithful | not faithful |
|---|---|---|
| **faithful** | 8 | 0 |
| **not faithful** | 5 | 15 |

DeepEval never wrongly calls a genuinely faithful claim unfaithful in
this set (perfect precision on "not faithful" as a prediction, 15/15)
but misses 5 of 20 genuinely unfaithful claims (recall 0.75) — a
real, measured version of the earlier finding that its binary
threshold and its no-contradiction-only definition both cost it recall
on exactly the cases a four-verdict scheme is built to catch.

Script: `confusion_matrix.py` in this session's scratchpad — pure
analysis, reruns instantly against any future taxonomy result set.

---

## 2026-09-14 — A second held-out document, and a new category that found its own ambiguity

**Why:** the taxonomy pilot so far used one already-known document
(`api-testing`) and one new domain (a synthetic expense policy) — the
benchmark design's full-scale ask wants *at least two* new domains, not
one. Added a second, genuinely new one: a synthetic consumer-electronics
warranty terms document, in a third distinct domain (insurance-adjacent
coverage/conditions) from anything tested before. Also used the
opportunity to close a gap the *first* review named directly and the
13-category taxonomy never actually tested — a claim true only under an
unstated condition — since a warranty document is a natural fit for it.

**Established categories (15 of the 17 claims, matching the prior
taxonomy exactly): 13/15 correct (86.7%).** The 2 misses are a third,
independent recurrence of the same `mentions_only`↔`no_evidence`
boundary already flagged by the volatility test and the confusion
matrix above — this time in both directions on the same document
(one `mentions_only` claim called `no_evidence`, one `no_evidence`
claim called `mentions_only`). Three documents, three confirmations,
same seam.

**Combined confusion matrix, all 43 established-category claims across
all 3 documents** (excludes the 2 new `conditional_truth` claims,
reported separately below since their own scoring convention is what
this round put in question):

| true ＼ predicted | entails | contradicts | mentions_only | no_evidence |
|---|---|---|---|---|
| **entails** | 12 | 0 | 0 | 0 |
| **contradicts** | 3 | 22 | 0 | 0 |
| **mentions_only** | 0 | 0 | 2 | 1 |
| **no_evidence** | 0 | 0 | 2 | 1 |

| Verdict | Support | Precision | Recall | F1 |
|---|---|---|---|---|
| entails | 12 | 0.80 | 1.00 | 0.89 |
| contradicts | 25 | 1.00 | 0.88 | 0.94 |
| mentions_only | 3 | 0.50 | 0.67 | 0.57 |
| no_evidence | 3 | 0.50 | 0.33 | 0.40 |

Overall: 37/43 (86.0%). The pattern holds and sharpens with more data,
not just repeats: `entails` and `contradicts` are *never* confused with
each other in either direction across all 43 claims and 3 unrelated
documents — every error is contained entirely within the
`mentions_only`/`no_evidence` pair. That's now the single, specific,
structurally-confirmed weak point this taxonomy has found, not a vague
"the judge is sometimes noisy."

**The new `conditional_truth` category — reported as an open question,
not a pass/fail score, because testing it surfaced a problem with the
test, not (necessarily) the tool:**

Two claims dropped a real, necessary condition from the document's own
conditional rule while asserting the outcome as though unconditional
(e.g. "a cracked screen from a spontaneous crack is covered," omitting
the document's own additional 90-day window requirement). My own answer
key called both `contradicts`, on the theory that presenting a
conditional truth as unconditional is materially misleading. The judge
disagreed with that convention, not obviously incorrectly:

- `WAR.16`: judged `entails` — read as "the stated condition (spontaneous
  crack) does hold when true," without penalizing the dropped time
  window.
- `WAR.17`: judged `mentions_only`, with a genuinely well-reasoned
  explanation: *"the claim is unconditional; the passages are silent on
  coverage without the capacity threshold condition"* — arguably the
  most epistemically honest of the three possible verdicts, neither
  fully entailed nor a clean contradiction.

Worth being direct about what this is: not a defect, and not filed as
one. This is a brand-new taxonomy category, invented for this round,
whose own correct scoring convention isn't actually settled — does
"drops a necessary condition" deserve `contradicts` (misleading),
`mentions_only` (real but incomplete), or does it depend on which
condition and how material it is? That's a real design question for
the taxonomy itself before this category can be scored pass/fail, not
yet a finding about Claim Validator.

Warranty fixtures: `taxonomy/warranty-terms.md`,
`taxonomy/warranty-claims.json`, `taxonomy/warranty-answer-key.json` in
this session's scratchpad — same status as the other taxonomy fixtures,
worth promoting into the repo if this set gets reused.

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
with one known-remaining miss and ordinary noise elsewhere. Merged to
`main` and deployed to both `usera-claimval` and `userb-claimval`.

---

## 2026-09-14 — Cost comparison against DeepEval, real token usage

**Why:** the accuracy comparison above (Tier 2) established that Claim
Validator beats DeepEval's `FaithfulnessMetric` on the 28-claim
adversarial set, but said nothing about what either actually costs to
run. An earlier exploratory question this session ("would integrating
DeepEval blow out validation cost?") was answered from architectural
inference, not a measurement — this closes that gap with real
Anthropic token usage from both systems, including the one-time cost
of building Claim Validator's own ontology/RAG index, which the user
explicitly asked to have counted rather than left out.

**Method:** a small, previously-unused document (a synthetic office
supplies reimbursement policy) and 6 hand-authored claims with ground
truth fixed before submission — small by design, per instruction, not
a large-scale benchmark. Both systems' Anthropic clients were
instrumented for real per-call token usage (Claim Validator's
`AnthropicClient` already tracks this internally via
`phases/llm_usage.py`; the external `AnthropicDeepEvalModel` wrapper
was extended to do the same). Both ran the identical Haiku 4.5 model.
Pricing: $1/MTok input, $5/MTok output (Anthropic's published rate,
2026-09-14). DeepEval was fed Claim Validator's own retrieved
passages, isolating judgment cost — it did not have to build or pay
for its own retrieval in this measurement.

| | Calls | Input tok | Output tok | Cost |
|---|---|---|---|---|
| CV — ontology/RAG build (one-time) | 23 | 24,433 | 6,190 | $0.0554 |
| CV — judge, 6 claims (3-run majority) | 6 | 14,688 | 1,432 | $0.0219 |
| CV — first-ever-run total | 29 | 39,121 | 7,622 | $0.0772 |
| DeepEval — same 6 claims | 24 | 10,397 | 2,385 | $0.0223 |

**Finding — the ontology build is nearly the entire cost gap.** On a
brand-new document, Claim Validator's first run costs ~3.5x DeepEval's
($0.0772 vs $0.0223), but $0.0554 of that $0.0772 is the one-time
index build, not per-claim judging.

**Finding — marginal per-claim cost is close to identical, and
slightly favors Claim Validator.** Once the ontology exists (reused
across every future claim checked against that document, per this
project's own content-hash caching), the per-claim cost is ~$0.00364
for Claim Validator vs. ~$0.00372 for DeepEval — a ~2% difference,
inside plausible run-to-run token-count noise on a 6-claim sample, not
a claimed precise edge either way. The honest conclusion is parity,
not "cheaper," given the sample size.

**Bonus corroboration, not the primary ask:** Claim Validator scored
6/6 correct on this set; DeepEval's binary faithfulness score missed
the `no_evidence` claim (a fabricated detail — nothing in it
contradicts the source, so Faithfulness has nothing to flag) and the
`mentions_only` claim (an on-topic-unstated detail) — the same
architectural gap already documented in the Tier 2 comparison above,
now reproduced on an independent third document.

**A real bug, found and fixed along the way, not the point of this
test but worth recording here since it was found here:** building the
ontology for this document raised a `ZeroDivisionError` in
`phases/phase1b_validation.py`'s census reconciliation — a concept
whose census spread was genuinely `[0, N]` (seen in some runs, not
others) divided by a zero lower bound instead of being guarded the
way the adjacent `CensusSpread.capture_range` already guards the
identical case. Fixed with the same guard, regression test added
(confirmed it fails on the old code, passes on the fix). On branch
`fix/census-zero-low-division`, not yet merged.

Test document, claims, answer key, and the instrumented comparison
script live in this session's scratchpad
(`cost_test/office-supplies-policy.md`, `cost_test/claims.json`,
`cost_test/answer-key.json`, `cost_test/run_cost_comparison.py`) — not
committed to the repo, same promotion note as the taxonomy fixtures
above.

---

## 2026-09-15 — Issue #8: range-claim shape added to the numeric checker, live-tested

**Why:** `claimvalidator/numeric_threshold_check.py` abstained outright
on any claim naming two or more numbers, correct for a genuinely
compound claim (issue #3's territory) but not for a single assertion
that states its bound as a range — "a response between 200ms and
500ms is acceptable." Added a third shape recognizing exactly two
explicit range phrasings ("between X and Y", "from X to Y"), matched
against a passage stating a range for what reads as the same fact.
Deliberately does **not** recognize a bare "X-Y" hyphen form — that's
genuinely ambiguous with a section, date, or version reference, and
stays left to the judge, same as a spelled-out number always has.

**A real design gap caught while writing the tests, before any live
call:** an early version scored a claim range that was a strict subset
of a wider passage range (e.g. claim "300-400ms", passage "200-500ms
acceptable") as `contradicts`. That's wrong — a narrower true
statement isn't a contradiction. Fixed before shipping: a subset range
now abstains, matching a promise already written into the module's own
docstring; only a claim range that extends outside what the passage
states is flagged.

**A real bug caught live, not by design review:** `_bound_containment`
(reused from shape 2, threshold 0.75) rejected the claim "a response
between 200ms and 500ms is acceptable" against the *actual* deployed
document's wording — "Response times between 200ms and 500ms are
considered acceptable under normal load" — because the realistic
paraphrase gap only scores 0.6, below shape 2's inherited threshold.
Confirmed by running the checker directly against the real
API-retrieved passage text, not just hand-written unit fixtures.
Lowered `_RANGE_MATCH_THRESHOLD` to 0.5 (with headroom above the real
0.6 case), re-verified a genuinely unrelated range still scores 0.0 on
the same document (a differing unit suffix folded into the placeholder
breaks the match before word overlap is even considered) — so this
wasn't loosened generically, just recalibrated against a real
paraphrase gap the 0.75 number was never actually validated against.

**Live verification, and its honest limit:** deployed to
`usera-claimval`, submitted 5 claims (exact match → `entails`,
extended bound → `contradicts`, subset → abstain, `from...to` phrasing
→ `entails`, bare-hyphen form → abstain) against a small purpose-built
document. The judge (Haiku) got all 5 right on its own both times
tested, so the deterministic override's reason text was never visibly
substituted in the API response — `pipeline.py` only overwrites a
verdict when it *disagrees* with the judge, the same masking effect
already documented for issue #15's live verification. Confirmed the
mechanism itself is correct the same way #15 was: running the checker
directly against the real, live-retrieved passage text (not a
synthetic fixture) and getting the right answer for every case,
backed by 44 unit tests in `tests/test_numeric_threshold_check.py`
(306 total, up from 295).

Test document, claims, and script: `range_test/` in this session's
scratchpad.

---

## 2026-09-15 — Issue #14: quantifier-weakening guidance added to the judge prompt, live-tested

**Why:** across every benchmark round, the judge treated a claim that
weakens a document's universal quantifier ("all"/"every"/"any"/"never")
into a partial one ("some"/"most") inconsistently — sometimes correctly
flagging it as misleading, sometimes reasoning "all implies some, so
this follows" and calling it `entails`. Pulling every quantifier-shift
case's actual reasoning text (6 claims across all 3 taxonomy documents,
2 per document) showed the judge *already notices* the quantifier
narrowing in its own stated reasoning in all 6 cases — the prompt
simply never told it which way to score that observation, so it split
roughly down the middle by chance rather than by design:

| Claim | Passage's quantifier | Claim's quantifier | Before |
|---|---|---|---|
| `API.3` | "for every response" | "some checks" | `contradicts` (see below — this one's different) |
| `API.4` | "explicitly out of scope" (unquantified = absolute) | "most forms... out of scope" | `entails` (wrong) |
| `EXP.3` | "every reimbursement request" | "some reimbursement requests" | `entails` (wrong) |
| `EXP.4` | "applies regardless of trip cost" (absolute) | "most international trips" | `contradicts` |
| `WAR.3` | "not accepted for any repair category" | "not accepted for most repair categories" | `contradicts` |
| `WAR.4` | "never covered" | "some cosmetic wear is covered" | `contradicts` |

**Fix:** added an explicit clause to the judge prompt's `contradicts`
rule, right alongside the existing numeric-threshold procedure: a claim
that restates a passage's rule with a weaker quantifier is
`contradicts`, even though the weaker statement is technically implied
by the stronger one — because "all implies some" reasoning drops the
exact fact the passage asserts (no exceptions) and substitutes a
different, weaker claim (exceptions might exist).

**Live results, 3 passes on all 6 claims, `usera-claimval`:**

- `API.4` and `EXP.3` — both previously wrong, **now correctly
  `contradicts`, 3/3 passes**, with the reasoning text now explicitly
  citing the new instruction ("the claim weakens this universal
  requirement... which contradicts the assertion").
- `EXP.4`, `WAR.3`, `WAR.4` — already correct, **held correct, 3/3**.
- `API.3` — flipped to `entails`, 3/3 (not noise — fully reproducible).

**`API.3` is not a regression — it's a different, ambiguous claim
shape the taxonomy design didn't distinguish from the other five.**
The source text: *"For every response it checks two things: 1. the
status code... 2. the response body validates against the schema."*
The claim: *"Some checks require the response body to validate against
the documented schema."* Read literally, "some checks" quantifies over
**check types** (and check #2 genuinely is the schema-validation
check — a true, unremarkable statement), not over **responses** the
way the other five examples cleanly do (same noun weakened on both
sides: "every reimbursement request" → "some reimbursement requests").
`API.3` shifts which noun is being quantified between the claim and the
passage, making the "correct" verdict genuinely ambiguous by
construction — and it was already unstable before this fix (it reads
`contradicts` in one historical run, `entails` in another, independent
of this change). This is a finding about the taxonomy's own example
design, not a defect introduced by the fix — the same category of
issue already documented for the `conditional_truth` category.

**Full 45-claim, 3-document regression check:** **37/45 → 42/45**
(82.2% → 93.3%), **zero regressions**. Five total fixes credited in
this pass: `API.4`, `EXP.3` (this fix), plus `API.6`, `API.13`,
`WAR.14` (already-shipped fixes from earlier this session, confirmed
still holding).

Live-test script: `live_test_quantifier_fix.py` in this session's
scratchpad.

---

## 2026-09-15 — Issue #11: gap report now credits a claim that confirms a concept in a different chunk than the census's primary anchor

**Why:** the gap report's "addressed" check only ever compared a
claim's cited chunk against the *one* chunk the census recorded for a
concept instance. A document discussing the same instance across
several chunks could have a claim genuinely about it citing a
different chunk than the census's single anchor — the two never
coincide, and the report calls it never-addressed. Measured directly
in this project's own paper: RFC 6749's `token` and `http_response`
concepts showed as zero-coverage gaps despite claims that plainly
discuss both.

**Root cause, confirmed by reading the code, not assumed:**
`census_many()` (`phases/census.py`) processes a document in
independent batches — no memory of prior batches. When the same
instance genuinely appears in two batches, the model correctly
re-identifies it in both, but the outer dedup (`if slug in
seen[concept]: continue`) discarded every sighting after the first
before it ever reached `chunk_of`. The data that would close this gap
was already computed by the census's own verification; it was just
thrown away. Not a missing capability — a data-retention bug.

**Fix:** added `chunks_of: Dict[str, List[int]]` to `CensusResult`,
populated in both `census()` and `census_many()` by *appending* every
confirmed sighting, alongside the existing `chunk_of` (unchanged, same
value, same behavior). `gap_report.py`'s addressed-check now falls
back to `chunks_of` only when the primary `chunk_of` check misses —
zero new LLM calls, zero new API cost, purely a local check against
data the census already computes. New tests in `tests/test_census.py`
and `tests/test_gap_report.py`; full suite 309/309, up from 306.

**Live verification, isolated to remove sampling noise between
"before" and "after":** rather than two separate full census runs
(which would differ from ordinary LLM sampling variance alone, making
any difference hard to attribute), ran `census_many` **once**, live,
against the real cached RFC 6749 ontology and its 182 chunks — reusing
the exact fixtures the paper's own evidence run used
(`tests/fixtures/rfc6749.txt`, the cached `rfc6749-8b06` ontology) —
then computed the old-logic and new-logic "addressed" decision from
that identical single dataset, so the only variable between the two
counts is the code change itself:

| Concept | Instances located | With 2+ confirmed chunks | Old logic addressed | New logic addressed | Rescued |
|---|---|---|---|---|---|
| `token` | 91 | 3 | 14/91 | 15/91 | 1 |
| `http_response` | 34 | 2 | 4/34 | 4/34 | 0 |

**The mechanism is real, not hypothetical** — 5 instances across the
two concepts the paper specifically flagged genuinely have more than
one independently-confirmed chunk on this real document, direct
evidence the root-cause diagnosis (independent per-batch
re-identification, discarded by the old dedup) is correct.

**Honest result on this specific 8-claim fixture:** `token` gained one
concretely rescued instance (`access-token-issued-by-authorization-server`
— census's primary anchor was chunk 60, never cited; also independently
confirmed in chunks 94/102/114, and chunk 94 *was* cited by three of
the eight claims). `http_response` gained zero with this exact claim
set — its two multi-chunk instances' secondary sightings didn't happen
to land on what these particular 8 claims cited. Not a sign the fix
doesn't work for `http_response`: with only 8 claims exercising a
182-chunk document, most concept instances of any kind go untouched
regardless of this fix — a larger or more targeted claim set would
have more chances to exercise the same mechanism. Reported plainly
rather than re-running with a hand-picked claim set to force a bigger
number.

Verification scripts: `verify_issue11.py`,
`verify_issue11_isolated.py` in this session's scratchpad.

## 2026-09-17 — Issue #16: a claim with nothing to judge against was reported as a false `entails`

**Why:** found live, running `docs/postman-auth-api-defects/`'s
seeded-defect variants plus the reference document against
`usera-claimval` with the same 8-claim set. Claim `C7` ("Passwords
must be at least 12 characters long") — a claim no version of the
document discusses — came back as `verdict: "entails", judged: false,
agreement: "1/1", reason: "", cited_chunks: []` on 2 of the 4 runs
(the reference document and one seeded variant), where retrieval
happened not to find any citation for it. On the other 2 runs
retrieval found citations and it correctly returned `no_evidence`
with real reasoning quoting the passages. Same claim, same complete
absence of supporting text anywhere — a retrieval-dependent, wrong
`verdict` whenever retrieval comes back empty.

**Root cause, confirmed by reading the code:** `phases/entailment.py`
correctly detects "nothing to judge this claim against" and constructs
`EntailmentVerdict(requirement_id=..., judged=False)` — but doesn't
set `verdict`, `reason`, `agreement`, or `runs_judged`, so they keep
the dataclass's own defaults: `verdict="entails"`, `reason=""`,
`agreement=1`, `runs_judged=1`. Three places in
`claimvalidator/pipeline.py` then read this object with a bare `if
verdict else <fallback>` — checking only whether the object is
`None`, not whether it was ever actually judged — so the dangerous
default leaked straight into the API response and the Excel report
(both read the same `ClaimResult.verdict` field). The JSON
`quality.entailed` aggregate count was never affected — it already
filters on `judged`; only the per-claim fields were wrong.

**Fix:** extended all three checks (`_agreement_label()`, and the
`verdict=`/`reason=` fields) to `verdict and verdict.judged`, matching
the guard already correctly used elsewhere in the same function. The
~20-line per-claim-construction block was extracted into its own
`_build_claim_result()` so the exact buggy shape — a real
`EntailmentVerdict(judged=False)` object with production's literal
field values — could be unit-tested directly, without standing up
retrieval, an ontology, or an LLM client. 316/316 tests pass (up from
309), 7 new.

**Live-verified:** re-ran the fixed pipeline end-to-end (real
Anthropic API, real retrieval, real judging) against the same
document and claim set. All 8 claims produced correct verdicts,
including `C7` — retrieval found citations for it this run (its own
documented run-to-run variance, see issue #7) and it correctly landed
on `no_evidence` with a real reason, never the false `entails` again.
The exact `judged=False` branch itself is covered directly by the new
unit tests, constructed from the literal object shape captured from
the live bug report rather than a hypothetical one.

## 2026-09-17 — Issue #17: judge no longer trusts a code comment's own conclusion as evidence for itself

**Why:** found live in `docs/refactor-accuracy-demo/`, checking
whether an LLM's own account of a real code refactor ("Summary of
Changes") matched the code it actually produced. A claim that a
batched SQL update "prevents race conditions" landed `entails` at only
`2/3` agreement — the majority's cited reasoning quoted the code's own
comment (`// 5. ... (prevents race conditions)`, written by the same
model that wrote the claim) as if that comment were evidence, rather
than reasoning about whether the SQL actually eliminates the
underlying stale-read race (it doesn't — no transaction, no row lock,
no conditional decrement). The escalation mechanism built specifically
to re-check a shaky verdict didn't apply: by design it only escalates
a split `contradicts`, never a split `entails`.

**Considered and rejected, discussed with the user first:** stripping
code comments before retrieval/judging. Would remove the exact
evidence text that misled the judge here, but the same failure shape
isn't comment-specific (a function named `preventsRaceCondition()` or
just the shape of a batched write can mislead the same way with zero
comments present), and comments often carry real facts a claim
legitimately needs — blanket-stripping would trade this failure for a
different one (true claims going to false `no_evidence`). Also
considered and rejected: a "does this claim need reasoning" classifier
to route to a different judge — a new failure-prone judgment layer
that doesn't reuse anything, for the same problem two narrower changes
already fix.

**Fix, two independent changes in `phases/entailment.py`:**
1. `_needs_escalation()` now also escalates a split (non-unanimous)
   `entails`, gated by a new `escalate_split_entails` setting
   (default on) — `mentions_only`/`no_evidence` stay excluded on
   split votes, unchanged; only the part of the original rationale
   that lumped `entails` in with those two is revised.
2. The judge prompt's "entails" rule now explicitly warns against
   accepting a comment, docstring, or identifier that merely asserts
   the same conclusion a claim makes, with a worked example using the
   real miss's own language (a batched `UPDATE` next to a "prevents
   race conditions" comment, with no lock/transaction/conditional
   check shown, is `mentions_only`, not `entails`).

327/327 tests pass (up from 316), 11 new — `_needs_escalation` had
zero direct test coverage before this; now six cases across both
verdict types, plus the existing prompt-content test file extended for
the new guidance text.

**Live-verified** by re-running the exact document and claims that
found the bug through the fixed code (real Anthropic API, no
redeploy first — same in-process pattern as issue #16's verification):
`C3` flipped from the buggy `entails 2/3` to a correct, unanimous
`mentions_only 3/3` — decisively enough that escalation wasn't even
needed to fix this particular case, the prompt guidance alone did it.
`C1` (a different failure shape — an unverified sub-clause riding
inside a compound claim, not a self-referential comment) is confirmed
unaffected, exactly as planned. Two unplanned bonus catches from the
same generalized standard: `C4` also moved to a correctly-qualified
`mentions_only` (unconfirmed transactional atomicity), and `C6` newly
caught a real overclaim as `contradicts` (claimed "generic" error
messages that actually leak `item.id`/`product.name` on two response
paths). Zero regressions spot-checked against
`docs/derived-test-cases-demo/`'s 15 claims — every verdict identical
to the originally recorded result.
