# Example: API testing — "Endpoint Contract Verifier"

A ready-to-use input pair for the
[OpenSpec → Claim Validator workflow](../../../../docs/openspec-workflow.md).

| File | Role |
|---|---|
| [`source-brief.md`](source-brief.md) | the **reference document** — the product brief the spec was written from. This is ground truth. |
| [`openspec/specs/contract-verifier/spec.md`](openspec/specs/contract-verifier/spec.md) | the **generated OpenSpec spec** — 11 requirements, in OpenSpec format, standing in for what an assistant would produce from the brief. |
| [`expected-claims.json`](expected-claims.json) | what the adapter turns that spec into (33 claims) — committed so you can see the output without running anything. |

## Deliberately seeded so validation is not all-green

The spec contains grounding defects on purpose, so running it through
Claim Validator produces a real mix of verdicts:

| Requirement | Spec says | Brief says | Expected verdict |
|---|---|---|---|
| Rate limiting (`R6`) | 25 requests/second | **10** requests/second | `contradicts` |
| Retry on transient failure (`R7`) | up to **three** retries, exponential backoff | retry **once**, no backoff | `contradicts` |
| Parallel execution (`R10`) | run 8 operations concurrently | *(brief says nothing about concurrency)* | `no_evidence` |
| Created-resource cleanup (`R11`) | delete resources it created | *(brief says nothing about cleanup)* | `no_evidence` / `mentions_only` |

The other ~7 requirements are faithfully grounded in the brief and should
come back `entails`.

## Run it

```bash
# from the repo root

# 1. spec -> claims
python3 tools/openspec-adapter/openspec_to_claims.py \
    tools/openspec-adapter/examples/api-testing/openspec/specs/ \
    -o claims.json --map claims.map.json --stats
# (this reproduces expected-claims.json)

# 2. validate the claims against the brief, via Claim Validator's API
#    (your own runner / Postman): upload source-brief.md, build an
#    ontology, submit claims.json, poll, download the report.
```

Or with the wrapper:

```bash
tools/openspec-adapter/run.sh tools/openspec-adapter/examples/api-testing ./out
```
