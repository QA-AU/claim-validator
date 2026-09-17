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
| [`validation-result.json`](validation-result.json) | the full real API response (`job_ab967b4da7f2`, `usera-claimval`, fresh ontology `doc-70ee995a-d1a7` — code isn't reused from any earlier bundle). |

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
