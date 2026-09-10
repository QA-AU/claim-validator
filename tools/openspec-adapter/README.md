# openspec-adapter

Normalize [OpenSpec](https://github.com/Fission-AI/OpenSpec) requirement
specs into [claim-validator](../../README.md) claim input.

It turns this:

```markdown
### Requirement: Error Handling
The command SHALL gracefully handle missing files and directories with appropriate messages.

#### Scenario: Missing changes directory
- **WHEN** `openspec/changes/` directory doesn't exist
- **THEN** display error: "No OpenSpec changes directory found. Run 'openspec init' first."
- **AND** exit with code 1
```

into this:

```json
[
  { "id": "cli-list.R6",
    "text": "The command SHALL gracefully handle missing files and directories with appropriate messages.",
    "source_ref": "spec.md › Requirement: Error Handling" },
  { "id": "cli-list.R6.S2.A1",
    "text": "When `openspec/changes/` directory doesn't exist, display error: \"No OpenSpec changes directory found. Run 'openspec init' first.\".",
    "source_ref": "spec.md › Requirement: Error Handling › Scenario: Missing changes directory › THEN[1]" },
  { "id": "cli-list.R6.S2.A2",
    "text": "When `openspec/changes/` directory doesn't exist, exit with code 1.",
    "source_ref": "spec.md › Requirement: Error Handling › Scenario: Missing changes directory › AND[2]" }
]
```

so claim-validator can judge each generated requirement against the source
document it was written from. See
[`docs/openspec-workflow.md`](../../docs/openspec-workflow.md) for the full
workflow and why you would do this.

## It is standalone, and not part of claim-validator

- **Pure Python standard library.** No third-party packages. Python ≥ 3.8.
- **No import of `claimvalidator`, `phases`, or `db`.** Its only knowledge
  of claim-validator is the shape of one input object: `{id, text,
  source_ref}`.
- **Not in the deployed image or the wheel.** `pyproject.toml` packages
  only `phases`, `db`, `claimvalidator`; `pytest` at the repo root only
  runs `tests/`. This directory is a dev tool that sits *in front of*
  claim-validator, invoked by hand or by a caller's own script.
- **No LLM call, anywhere.** Deliberate: the generated specs are the thing
  under test, so the step that prepares them for judging must not itself
  be a model that could quietly correct or distort them. Every
  transformation here is a deterministic rule (see
  [what it changes](#what-it-changes-vs-raw-openspec-output) below).

Moving it to its own repository later is a `git mv` — nothing depends on
its location.

## Usage

```
python openspec_to_claims.py PATH [PATH ...] [options]
```

`PATH` is an OpenSpec `spec.md` file, or a directory searched recursively
for `spec.md`. Full specs (`## Requirements`) and delta specs
(`## ADDED / MODIFIED / REMOVED Requirements`) are both handled; the mode
is auto-detected per file.

| Option | Meaning |
|---|---|
| `--granularity {assertion,scenario,requirement}` | `assertion` (default): one claim per `THEN`/`AND` bullet, plus one per requirement's `SHALL` statement. `scenario`: one compound claim per scenario. `requirement`: only the `SHALL` statement. |
| `--capability NAME` | Override the capability id used in claim ids / provenance (default: the `spec.md`'s parent directory name). |
| `--include-removed` | Also emit `## REMOVED Requirements` (skipped by default — a removal has nothing to ground-check). |
| `--format {json,csv}` | Output format (default: `json`). CSV columns are `id,text,source_ref`. |
| `--map PATH` | Also write a provenance map: `{claim_id: {file, requirement, scenario, marker, delta_op, text}}`. |
| `-o, --output PATH` | Write output here (default: stdout). |
| `--stats` | Print a one-line summary to stderr. |

### Typical run

```bash
# 1. adapter: OpenSpec change specs -> claims
python tools/openspec-adapter/openspec_to_claims.py \
    openspec/changes/add-auth/specs/ \
    --map claims.map.json -o claims.json --stats

# 2. runner (your own ~50-line script, or Postman): send claims.json plus
#    the source brief to claim-validator's existing API, poll, download the
#    report. The adapter does not do this step and does not know the API.
```

### Choosing granularity

Default to `assertion`. claim-validator's known compound-claim retrieval
gap (issue #3) is triggered by bundling several checkable statements into
one claim; splitting to the `THEN`/`AND` bullet keeps each claim atomic.
Use `scenario` or `requirement` only to compare, or when you specifically
want the coarser reading.

## What it changes vs. raw OpenSpec output

It is **not** a pure format re-wrap. In order of how much they matter:

1. **Splitting** — one requirement becomes many claims: its `SHALL`
   sentence, plus one claim per `THEN`/`AND` bullet. Fragments the
   content; does not alter it.
2. **WHEN-folding** — each bullet-claim gets its scenario's `WHEN`
   prepended as a condition clause, so the claim stands alone.
3. **Markdown stripping** — `**WHEN**`/`**THEN**` markers removed, bullets
   and headings dropped, newlines collapsed. Backticked code spans kept
   verbatim.
4. **Template re-sentencing** — `WHEN … / THEN …` becomes
   `"When …, …."` via a fixed template (lowercase the spliced verb, one
   trailing period). Mechanical; no semantic change.
5. **`## REMOVED` dropped** by default; **`## MODIFIED` uses the new
   text**, and its `source_ref` is tagged `MODIFIED` — the reference
   document you validate against should then include the prior spec state
   too, or modified requirements read as spurious `no_evidence`.
6. **Ids + `source_ref` added** from structure, for tracing a verdict back.

It does **not**: deduplicate, drop "obvious" claims, resolve ambiguity,
rephrase semantically, call a model, or express any opinion on verdicts.

## Tests

```bash
cd tools/openspec-adapter && python -m pytest -q
```

Includes golden-file tests: `tests/fixtures/cli-list/spec.md` (a real
OpenSpec capability spec) and `tests/fixtures/sample-change/spec.md` (a
synthetic delta) are checked against committed expected output, so an
OpenSpec format change breaks a test here rather than silently producing
wrong claims downstream.
