# Example: UI testing — "Visual Regression Gate"

A ready-to-use input pair for the
[OpenSpec → Claim Validator workflow](../../../../docs/openspec-workflow.md).

| File | Role |
|---|---|
| [`source-brief.md`](source-brief.md) | the **reference document** — the product brief the spec was written from. This is ground truth. |
| [`openspec/specs/visual-regression-gate/spec.md`](openspec/specs/visual-regression-gate/spec.md) | the **generated OpenSpec spec** — 11 requirements, in OpenSpec format, standing in for what an assistant would produce from the brief. |
| [`expected-claims.json`](expected-claims.json) | what the adapter turns that spec into (33 claims) — committed so you can see the output without running anything. |

## Deliberately seeded so validation is not all-green

| Requirement | Spec says | Brief says | Expected verdict |
|---|---|---|---|
| Difference threshold (`R4`) | fail above **1%** of pixels | fail above **0.1%** | `contradicts` |
| Baseline update policy (`R7`) | **auto-update** when diff < 2% | **never** auto-update — always `--update-baselines` | `contradicts` |
| Device pixel ratio (`R8`) | capture at **2x** | capture at **1x**, 2x is out of scope | `contradicts` |
| Accessibility check (`R9`) | run axe-core, fail on serious violations | *(brief lists a11y as out of scope)* | `no_evidence` |
| Cross-browser capture (`R10`) | also capture in Firefox and WebKit | *(brief says Chromium only, cross-browser out of scope)* | `no_evidence` |

The other ~6 requirements are faithfully grounded in the brief and should
come back `entails`.

## Run it

```bash
# from the repo root

# 1. spec -> claims
python3 tools/openspec-adapter/openspec_to_claims.py \
    tools/openspec-adapter/examples/ui-testing/openspec/specs/ \
    -o claims.json --map claims.map.json --stats
# (this reproduces expected-claims.json)

# 2. validate the claims against the brief, via Claim Validator's API
#    (your own runner / Postman): upload source-brief.md, build an
#    ontology, submit claims.json, poll, download the report.
```

Or with the wrapper:

```bash
tools/openspec-adapter/run.sh tools/openspec-adapter/examples/ui-testing ./out
```
