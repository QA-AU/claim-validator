# Example: a real earnings report, and the ontology cache bug it surfaced

Two separate findings from one document. The claim-generation result
is clean — the more important finding is a real, reproducible
infrastructure bug hit along the way.

| File | Role |
|---|---|
| [`source-costar-q2-2026-earnings.txt`](source-costar-q2-2026-earnings.txt) | the reference document — CoStar Group, Inc.'s (NASDAQ: CSGP) real Q2 2026 earnings press release, sourced verbatim from [SEC EDGAR](https://www.sec.gov/Archives/edgar/data/1057352/000105735226000061/q2fy262026earningspressr.htm) (Exhibit 99.1 to Form 8-K, filed July 28, 2026). |
| [`generated-claims.md`](generated-claims.md) | the exact prompt and the model's raw output — 25 claims, unedited. |
| [`claims.json`](claims.json) | the same 25 claims as `{id, text}`. |
| [`validation-result.json`](validation-result.json) | the full real API response (`job_7404b791fbac`, `usera-claimval`, ontology `doc-833b4220-430d`). |

## The claim-generation result: clean, again

```
25 claims submitted, 25 judged, 3/3 agreement on every one
  25 entails
   0 mentions_only / contradicted / no_evidence
```

Manually verified against the source before submission — all 25 are
accurate. This is the **third** attempt this session at eliciting an
organic mistake through direct restatement (after a small API spec
and a 40-claim detailed breakdown), and the third clean result.
Density of similar numbers alone — revenue, EBITDA, Adjusted EBITDA,
net income, EPS, Adjusted EPS, five different YoY percentages in one
paragraph — isn't enough to trip a capable model that has the
document directly in front of it. (The technique that finally did
work is documented separately in
[`../derived-test-cases-demo/`](../derived-test-cases-demo/) — asking
for *derived* claims instead of restatement.)

## The real finding: a failed first build permanently bricks that document's content hash

Building the ontology for this document **failed silently on the
first attempt** — `POST /api/ontologies` returned a normal 202-shaped
response (`job_a8aceb85ee4e`), but the background extraction never
completed. No error appeared anywhere in the container logs. Five
minutes later, the ontology's own metadata showed `has_index: false`,
`concept_types: 0` — created, but never actually built.

**The bug:** retrying the exact same upload doesn't retry the build.
It returns `{"key": "doc-ce4bd09a-f261", "reused": true}` — the
system treats the empty, never-extracted record as a complete,
reusable ontology, forever. Confirmed by reading
`claimvalidator/document_identity.py::resolve_ontology_key()`:

```python
digest = content_hash(document_paths)
existing = store.find_by_content_hash(digest)
if existing:
    return existing.key, True   # <-- no check that it actually finished
```

There is no completeness check — any record with a matching content
hash is treated as done, whether or not it ever built an index. Since
ontologies have no delete or rebuild route by design (see
`docs/known-fixes.md` and issue #11's plan notes on immutability),
**a single transient failure on a document's first-ever build
permanently and silently poisons that exact content forever.** The
only workaround is changing the document's bytes — which defeats the
entire point of content-hash caching, since the "same" document can
never be successfully built again after one bad first attempt.

**Confirmed as transient, not deterministic:** re-uploading a
byte-different copy (one trailing newline added → new content hash
`833b4220-430d`) built successfully on the first try — real
extraction, 8 concept types, 142 instances (`financial_metric`,
`reporting_period`, `business_segment`, `product_or_service`,
`company_entity`, `non_gaap_adjustment`, `market_opportunity`,
`performance_achievement`). Nothing about this specific document
content is unbuildable; the original failure was a one-off glitch,
which is exactly what makes the caching gap dangerous — any random
transient failure, on any document, on its first build, is permanent
and invisible until someone notices the ontology never actually has
data in it.

Filed as [issue #18](https://github.com/QA-AU/claim-validator/issues/18).

## Run it

```bash
curl -s -X POST https://<tenant>.azurecontainerapps.io/api/documents \
  -H "Authorization: Bearer <token>" \
  -F "file=@docs/costar-q2-2026-earnings-demo/source-costar-q2-2026-earnings.txt"

curl -s -X POST https://<tenant>.azurecontainerapps.io/api/ontologies \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"files": ["<path from the upload response>"]}'

curl -s -X POST https://<tenant>.azurecontainerapps.io/api/validations \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d "{\"document\": {\"files\": []}, \"claims\": $(cat docs/costar-q2-2026-earnings-demo/claims.json), \"ontology_key\": \"<key from the ontology response>\"}"
```
