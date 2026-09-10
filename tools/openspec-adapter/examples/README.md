# Example inputs

Two ready-made input pairs for the
[OpenSpec → Claim Validator workflow](../../../docs/openspec-workflow.md).
Each pair is a **source brief** (ground truth) plus a **generated OpenSpec
spec** written from it, so you can run the whole pipeline end to end
without having OpenSpec installed or a real project to hand.

| Folder | Domain | Capability | Claims |
|---|---|---|---|
| [`api-testing/`](api-testing/) | API testing | `contract-verifier` — checks a live service against its OpenAPI document | 33 |
| [`ui-testing/`](ui-testing/) | UI testing | `visual-regression-gate` — screenshot-diff CI gate for a web UI | 33 |

Both specs have a handful of **deliberately planted grounding defects**
(a threshold that disagrees with the brief, a requirement the brief never
mentions, …), listed in each folder's own README, so validating them
produces a real mix of `entails` / `contradicts` / `no_evidence` rather
than an all-green run.

## Use one as input

```bash
# from the repo root — spec -> claims.json
python3 tools/openspec-adapter/openspec_to_claims.py \
    tools/openspec-adapter/examples/api-testing/openspec/specs/ \
    -o claims.json --map claims.map.json --stats
```

Then send `claims.json` plus that folder's `source-brief.md` to Claim
Validator's API (upload the brief, build an ontology, submit the claims,
poll, download the report). The wrapper does the adapter half in one step:

```bash
tools/openspec-adapter/run.sh tools/openspec-adapter/examples/ui-testing ./out
```

`expected-claims.json` in each folder is the committed adapter output —
compare against it, or just read it to see the shape.
