# Example: does the refactor actually do what the LLM says it did?

A different shape of test from every other bundle in this repo. Every
one of them checked claims about *prose* — a spec, a report — against
the same prose. This one checks an LLM's *own account of its own code
change* against the code it actually produced: a real copilot
workflow (ask an assistant to refactor something, get code back plus
a summary of what changed), with Claim Validator used to answer "is
the summary actually true of the code?"

It found the most substantive issue of the whole session — not a
generation mistake, a **judge** limitation.

| File | Role |
|---|---|
| [`original-checkout.js`](original-checkout.js) | the starting code — an Express checkout endpoint with four annotated issues (legacy JWT verification, a stock-decrement race condition, a stale third-party API note, fragile error handling). Not used as ground truth — see below. |
| [`model-refactor-response.md`](model-refactor-response.md) | the exact refactor prompt, the model's full refactored code, and its own numbered "Summary of Changes" — unedited. |
| [`refactored-checkout.js`](refactored-checkout.js) | the refactored code, extracted from the response above. **This is the reference document** — the claims are checked against what the new code actually does, not the old code. |
| [`claims.json`](claims.json) | the model's own 6 summary points, turned into `{id, text}` claims verbatim. |
| [`validation-result.json`](validation-result.json) | the full real API response (`job_ab967b4da7f2`, `usera-claimval`, fresh ontology `doc-70ee995a-d1a7` — code isn't reused from any earlier bundle). This is the result that found issue #17 — kept as-is, not overwritten. |
| [`validation-result-after-fix.json`](validation-result-after-fix.json) | the same document and claims, re-run locally against the fixed code once #17 was fixed — see "Fixed — issue #17" below. |
| [`validation-result-after-clause-fix.json`](validation-result-after-clause-fix.json) | the same document and claims, re-run again after the two #17 follow-ons (per-clause verification, the concurrency checker) — see "Fixed further — per-clause verification and a concurrency checker" below. |

## The task given to the model

> Refactor this endpoint. I want to optimize the database calls to be
> faster, update the JWT verification to use modern jose library
> syntax instead of legacy jsonwebtoken, and make sure it can handle
> concurrent user checkouts safely.

`claude-haiku-4-5` produced real, running-shaped code and a confident
six-point summary of what it changed. That summary became the claims.

## What actually happened

```
6 claims submitted, 6 judged
  5 entails (one at only 2/3 agreement)
  1 mentions_only
  0 contradicts
```

Clean-looking on the surface. It isn't.

## Finding 1 — the judge cited the code's own comment as evidence for the same claim it was checking

**`C3`:** *"The refactored code implements atomic batch stock updates
using a SQL CASE statement to prevent race conditions where
concurrent checkouts could oversell inventory."*

**Real verdict: `entails`, but only `2/3` agreement** — one of the
three independent judge runs actually disagreed.

The majority's own cited reasoning:
> "Passages show '5. Execute all stock updates in a single batch
> query **(prevents race conditions)**' with `UPDATE products SET
> stock = CASE id ... END WHERE id = ANY(...)` performing atomic
> batch updates"

The code does not actually prevent the race condition. It still
reads `product.stock`, checks it, computes `newStock = stock - qty`,
and later writes that precomputed absolute value — no transaction, no
row lock (`SELECT ... FOR UPDATE`), no atomic conditional decrement
(`stock = stock - $qty WHERE stock >= $qty`). Batching six separate
`UPDATE` calls into one SQL statement changes nothing about the
underlying time-of-check-to-time-of-use race: two concurrent
checkouts can still both read the same stale stock value, both pass
validation, and both write it — oversold inventory, the exact bug the
refactor was asked to fix.

The judge's cited "evidence" is the code's own comment
(`// 5. Execute all stock updates in a single batch query (prevents
race conditions)`) — written by the same model, asserting the same
unverified claim. The judge matched the claim against a comment that
says the same thing, rather than reasoning about whether the SQL
actually delivers that property.

## Finding 2 — a compound claim's false half rode along with its true half, unanimously

**`C1`:** *"The refactored code replaced jsonwebtoken with the jose
library for JWT verification, using async/await support and better
cryptographic defaults."*

**Real verdict: `entails`, 3/3 unanimous** — no internal disagreement
at all, which is worse than `C3`.

The jose swap and `async`/`await` are genuinely true. But the
original code explicitly pinned `algorithms: ['HS256']`; the
refactored `jwtVerify(token, secret)` call drops that restriction
entirely — arguably a regression (broader algorithm acceptance is a
real category of JWT vulnerability), not "better ... defaults." The
judge verified the parts it could textually match (library name,
`await` keyword) and let the unverifiable — arguably false — clause
ride inside the same sentence, with full agreement.

## Fixed — issue #17

Two changes in `phases/entailment.py`: the judge prompt now explicitly
warns against accepting a comment or identifier that merely asserts
the same conclusion a claim makes, and escalation to a stronger model
now also covers a split (non-unanimous) `entails`, not just a split
`contradicts`, so a case like `C3` — which showed real internal
disagreement (`2/3`) that the original design had no way to act on —
gets a second opinion. Full plan and rationale, including why
stripping code comments was considered and rejected, in issue #17's
own thread.

**Live-verified by re-running this exact document and claim set
through the fixed code:**

```
C1  entails       3/3   (unaffected, as expected — different failure shape)
C2  entails       3/3
C3  mentions_only 3/3   (was: entails, 2/3 — the fix's direct target)
C4  mentions_only 3/3   (was: entails, 2/3 — same overclaim pattern, unplanned bonus catch)
C5  entails       3/3
C6  contradicts   3/3   (was: entails, 3/3 — a genuine overclaim the new standard also caught)
```

`C3` flipped cleanly to the correct verdict — and decisively enough
(unanimous `mentions_only`) that escalation wasn't even needed to
correct it; the prompt guidance alone did it. `C1` is confirmed
unaffected, exactly as planned — its problem (an unverified sub-clause
in a compound claim) is a different shape this fix doesn't claim to
solve. `C4` and `C6` are welcome, unplanned improvements: the same
"trace the actual mechanism, don't accept a self-description" standard
generalized to two nearby overclaims (unconfirmed transactional
atomicity; "generic" error messages that actually leak
`item.id`/`product.name` on two paths) that the original run had
missed entirely. Zero regressions on
[`../derived-test-cases-demo/`](../derived-test-cases-demo/)'s 15
claims — every verdict identical to the originally recorded result.

## Fixed further — per-clause verification and a concurrency checker

`C1` was confirmed unaffected by #17's fix, exactly as planned there —
its problem is a different shape (a false clause riding inside an
otherwise-true compound sentence, not a self-referential comment).
Two follow-ons close it:

1. **Per-clause re-verification** (`phases/entailment.py`) — a claim
   that reads `entails` as a whole sentence and looks compound (any
   plain " and ", looser than the retrieval-only splitter in
   `claim_retrieval.py`) gets each clause judged independently against
   the same cited passages. Any clause short of `entails` downgrades
   the whole claim.
2. **A deterministic concurrency checker**
   (`claimvalidator/concurrency_claim_check.py`), modeled on
   `numeric_threshold_check.py`'s own shape — a claim asserting
   race-safety with no recognized synchronization marker (transaction,
   lock, guarded conditional update) in the passages downgrades to
   `mentions_only`, independent of whichever way the LLM judge itself
   happened to land.

**Live-verified**, full 6-claim re-run against the same document:

```
C1  mentions_only 3/3   (was: entails, 3/3 — fixed; clause breakdown shows "better cryptographic
                         defaults" unconfirmed, the jose/async-await half correctly stays entailed)
C2  mentions_only 3/3   (was: entails, 3/3 — same standard also catches "improving performance"
                         as unconfirmed; see the honest caveat below)
C3  mentions_only 3/3   (was: entails, 2/3 — #17's prompt fix alone got it right this run;
                         neither new mechanism needed to fire)
C4  mentions_only 3/3
C5  entails       3/3   (unaffected — not compound, no concurrency-safety claim)
C6  contradicts   3/3
```

**A real regression, found during this verification, fixed before
calling it done:** the first version of the per-clause splitter judged
each clause on its bare text alone. On a completely different bundle
([`../derived-test-cases-demo/`](../derived-test-cases-demo/)'s `T7`,
"the response contains the user's username **and** email matching the
account that generated the token"), splitting at "and" produced
`"...username"` and `"email matching the account..."` — and the second
fragment lost the shared subject the first clause carried, reading as
unconfirmable on its own. A previously-correct `entails` flipped to a
false `mentions_only`. Fixed by carrying the full original sentence
alongside each clause as context, rather than judging the bare
fragment alone. Re-verified: `T7` back to `entails`, `C1` still
correctly downgraded — the catch survived, the false positive didn't.

One honest side effect, not hidden: `C2` also moved to `mentions_only`
— stricter than before, since "reduces round trips" and "improves
performance" are treated as two separately-confirmable things now, and
the passages only state the first. Defensible (performance improvement
genuinely isn't stated, only strongly implied), but a real behavior
shift worth knowing about, not just an unambiguous win.

## Why this is a judge finding, not a generator finding

Every other bundle in this repo is about whether the *generator*
(the model writing claims) makes mistakes. This one shows the
*judge* accepting a claim about code because the claim's wording
echoes something already written in the code — a comment, a
variable name — without independently reasoning about whether the
code's actual control flow and logic deliver what's claimed. That
gap doesn't show up on prose documents, where there's no
"self-describing comment" for a claim to echo; it shows up
specifically when the reference document is source code that
narrates its own intent.

Filed as [issue #17](https://github.com/QA-AU/claim-validator/issues/17).

## Run it

```bash
curl -s -X POST https://<tenant>.azurecontainerapps.io/api/documents \
  -H "Authorization: Bearer <token>" \
  -F "file=@docs/refactor-accuracy-demo/refactored-checkout.js;type=text/plain;filename=refactored-checkout.txt"
# note: the API rejects .js by extension; upload it as .txt (same content)

curl -s -X POST https://<tenant>.azurecontainerapps.io/api/ontologies \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"files": ["<path from the upload response>"]}'

curl -s -X POST https://<tenant>.azurecontainerapps.io/api/validations \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d "{\"document\": {\"files\": []}, \"claims\": $(cat docs/refactor-accuracy-demo/claims.json), \"ontology_key\": \"<key from the ontology response>\"}"
```
