# Claim Validator

Given a reference document and an external tool's or person's output — a
bare list of claims, just `id` + text — independently judges whether each
claim is actually supported by the document, and separately reports what
the claims never address at all.

Built on top of the ontology + RAG + entailment-judge machinery from
[llm-rag-ontology-eval-scaffold](../llm-rag-ontology-eval-scaffold), reused
here as a library rather than rewritten. See
[`../.claude/plans/shiny-floating-wilkes.md`](~/.claude/plans/shiny-floating-wilkes.md)
for the design this repo was built from.

## What it does

1. Builds or reuses an ontology + RAG index for the reference document
   (cached automatically by document content — resubmitting the same bytes,
   under any name, never re-extracts).
2. For each claim with no citation, retrieves supporting passages from the
   document.
3. Runs a free, deterministic shape check (has enough text to judge at all).
4. Runs the entailment judge (3-run majority verdict: `entails` /
   `mentions_only` / `contradicts` / `no_evidence`) against what was
   retrieved.
5. Separately produces a gap report: using the document's own census
   (an exhaustive, repeated count — reported as a range, never a single
   number) as ground truth, reports which concept instances no claim's
   citation ever touches. Kept apart from per-claim verdicts on purpose — a
   claim set can be perfectly correct and still leave this report looking
   the same.

What it explicitly does not do: generate requirements or claims (validate-
only — see the plan for why), or execute anything against a real system.

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env.local   # then edit as needed

# Smallest path — no DB, no HTTP, prints a JSON report:
.venv/bin/python scripts/validate_claims.py \
    tests/fixtures/trial_protocol.txt \
    tests/fixtures/trial_claims.csv \
    --provider ollama --model qwen3.8:latest
```

## Running the API

```bash
.venv/bin/uvicorn claimvalidator.api:app --reload
```

`POST /api/validations` returns a `job_id` immediately (never blocks on the
underlying multi-minute judge/census work); poll `GET
/api/validations/{job_id}` for status and, once `status: "done"`, the full
report — per-claim verdicts plus the separate `gap_report` section.
`POST /api/ontologies` lets a caller pre-warm or inspect an ontology
independently of any validation run.

All `/api/*` routes except `/api/ping` require a token — `Authorization:
Bearer <token>` or an `X-API-Token` header. If `CLAIMVAL_API_TOKEN` is
unset, one is generated and printed at startup (see `phases/api_auth.py`).

## Layout

- `phases/` — copied from the source repo (verbatim, except two additive
  edits noted in their own docstrings: `ontology_store.py`'s content-hash
  caching, `phase1_storage.py`'s one import redirected to the extracted
  `run_report_stem.py`).
- `claimvalidator/` — everything new: the retrieval adapter, the duck-typed
  shims that let a bare `id + text` claim satisfy `judge_entailment`'s and
  `check_requirement_shapes`'s existing contracts, the gap report, the
  async job/API layer.
- `scripts/validate_claims.py` — the no-DB, no-HTTP path; also the script
  that proved the shims against a real model before anything else was built
  on top of them.

## Tests

```bash
.venv/bin/pytest tests/ -q
```

A mix of tests ported unmodified from the source repo (`test_census.py`,
`test_ontology_store.py`, `test_run_tracker.py` — proving the reused logic
still behaves as documented) and new ones for this repo's own code
(`test_claim_shims.py`, `test_claim_retrieval.py`, `test_document_identity.py`,
`test_gap_report.py`, `test_entailment_shim.py`,
`test_requirement_shapes_shim.py`, `test_jobs.py`, `test_api.py`).

`test_entailment_shim.py` is the automated version of this repo's central
risk: does a bare claim's shim actually satisfy `judge_entailment`'s
duck-typing against a real call, not just look right in isolation. The
mocked version runs in CI; `scripts/validate_claims.py` against a real
model is what actually settled the question before this was trusted.
