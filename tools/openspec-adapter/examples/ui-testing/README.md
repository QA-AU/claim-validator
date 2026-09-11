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
| Accessibility check (`R10`) | run axe-core, fail on serious violations | lists accessibility auditing as **explicitly out of scope** | `contradicts` |
| Cross-browser capture (`R11`) | also capture in Firefox and WebKit | lists cross-browser (Firefox/WebKit) as **explicitly out of scope** — Chromium only | `contradicts` |

`R10`/`R11` are `contradicts`, not `no_evidence`: the brief doesn't just
omit accessibility auditing and cross-browser capture — its "Explicitly
out of scope for v1" section actively excludes them by name. A requirement
adding a feature the source document explicitly rules out conflicts with
it; `no_evidence` is only the right verdict when the source is silent on a
topic, not when it has said no to it. (An earlier version of this table
had both wrong on that basis, live-verified: the tool correctly returned
`contradicts` 3/3 for both, quoting the "out of scope" line back.)

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
