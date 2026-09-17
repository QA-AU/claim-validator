# Example: derived test cases — the technique that actually worked

Three earlier attempts this session (direct claim restatement on a
small API spec, on a 40-claim detailed breakdown, and on a real dense
financial report) all came back essentially clean — a capable model
given direct document access is genuinely hard to trip into a factual
error. This bundle is the one technique that broke that pattern: ask
for **derived test cases** (boundary conditions, cross-call
synthesis) instead of direct restatement, on the exact same document
already used in
[`../postman-auth-api-defects/`](../postman-auth-api-defects/) — no
new ontology needed, it's byte-identical to
`reference-original.txt` there.

| File | Role |
|---|---|
| [`source-simple-auth-api.txt`](source-simple-auth-api.txt) | the reference document — identical to `../postman-auth-api-defects/reference-original.txt`. |
| [`generated-claims.md`](generated-claims.md) | the exact prompt and the model's raw output — 15 derived test-case claims, unedited. |
| [`claims.json`](claims.json) | the same 15 claims as `{id, text}`. |
| [`validation-result.json`](validation-result.json) | the full real API response (`job_1bd67de4b061`, `usera-claimval`, ontology `doc-e2719557ee47-9d32` reused). |

## What actually happened

```
15 claims submitted, 15 judged, 3/3 agreement on every one
   6 entails
   8 mentions_only
   1 contradicts
   0 no_evidence
```

## The genuine catch — no seeding

**`T15`:** *"When a request is submitted without the X-Request-ID
header to any endpoint, the API either returns 400 Bad Request or
accepts it and generates a tracking ID internally (depending on
whether X-Request-ID is strictly required vs. optional)."*

**Real verdict: `contradicts`, 3/3 unanimous.**
> "The Requirements section states 'All endpoints require X-Request-ID
> header for tracking', which specifies it is required. The claim
> presents X-Request-ID as either strictly required or optional
> (allowing internal generation), contradicting the passage's
> assertion that it is mandatory."

Asked to derive a test case rather than quote the spec, the model
hedged on a boundary condition the document never actually leaves
ambiguous — inventing an "or maybe it's optional and
auto-generated" branch for a header the spec flatly requires
everywhere. A genuine test-generation mistake, the kind that slips
into a real QA copilot's test plan unnoticed.

## The `mentions_only` majority — plausible synthesis, not stated

8 of 15 claims (`T2`, `T4`, `T5`, `T6`, `T9`, `T11`, `T13`, `T14`)
landed on `mentions_only` — reasonable inferences about rate-limit
windowing, header values at the exact boundary, post-logout token
invalidation, and cross-endpoint bucket-sharing, none of which the
spec states explicitly. This is the more common organic failure mode
this technique surfaces: not wrong facts, but confident specifics the
source never actually committed to.

## Why this worked when direct restatement didn't

Direct restatement lets a capable model just transcribe what's
already written — accurate by construction. Asking it to synthesize a
boundary condition or a multi-step sequence forces it to reason
*beyond* the text, which is exactly where a real, uncoached mistake
has room to happen. See the parent session's other three attempts
(`../llm-generated-requirements-demo/`,
`../costar-q2-2026-earnings-demo/`) for the direct-restatement
baseline this contrasts with.

## Run it

Same shape as the other bundles — this one reuses the existing
cached ontology, no rebuild:

```bash
curl -s -X POST https://<tenant>.azurecontainerapps.io/api/validations \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d "{\"document\": {\"files\": []}, \"claims\": $(cat docs/derived-test-cases-demo/claims.json), \"ontology_key\": \"doc-e2719557ee47-9d32\"}"
```
