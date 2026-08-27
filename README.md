# Claim Validator

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Runs an LLM's output — a batch of claims it made about a reference
document, just `id` + text — back through an independent check: is each
one actually supported by the document, or does it just sound right? This
exists to answer that question at scale, so you can decide whether to
trust a given LLM's output on a document before anyone acts on it, rather
than spot-checking a few claims by hand. Also works on claims from any
other tool or a person — the check doesn't care where the claims came
from, only whether the document backs them up.

It separately reports what the claims never address at all, since a
claim set can be perfectly accurate and still leave real gaps.

Built on top of the ontology + RAG + entailment-judge machinery from
[llm-rag-ontology-eval-scaffold](../llm-rag-ontology-eval-scaffold), reused
here as a library rather than rewritten. See
[`../.claude/plans/shiny-floating-wilkes.md`](~/.claude/plans/shiny-floating-wilkes.md)
for the design this repo was originally built from.

For the API itself — endpoints, request/response shapes, verdict types,
the gap report, the Excel report's sheets — see the
**[user manual](https://qa-au.github.io/claim-validator/manual.html)**
(source: [docs/manual.html](docs/manual.html), GitHub shows `.html` files as
source rather than rendering them). This file covers what the manual
doesn't: how the pieces fit together, how it's deployed, and what's tested.

## What it does

1. Builds or reuses an ontology + RAG index for the reference document
   (cached automatically by document content — resubmitting the same bytes,
   under any name, never re-extracts; see [Ontologies](#ontologies) below
   for the full lifecycle).
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
only), or execute anything against a real system.

## Compared to DeepEval

[DeepEval](https://github.com/confident-ai/deepeval) and this tool both use
an LLM as judge to check whether text is actually backed by a source —
DeepEval's `Faithfulness`/`Hallucination` metrics answer close to the same
question this tool's entailment judge does (`entails` / `contradicts` /
`no_evidence`). Where they differ:

- **What you bring to it.** DeepEval is a Pytest-style testing library —
  you write test cases, pick metrics, and run them locally as part of your
  own suite. This tool is a hosted, multi-tenant API: `POST` a document and
  a bare list of `id + text` claims, poll for the report. There's no
  test-writing step; the claims themselves are the input, not code.
- **Grounding.** DeepEval's RAG metrics score against whatever context you
  hand them at eval time. This tool builds its own ontology + RAG index
  from the document first (cached by content hash, reused across
  submissions — see [Ontologies](#ontologies)), so retrieval is grounded in
  a structured extraction of the document itself, not raw chunks supplied
  per test run.
- **Coverage, not just correctness.** DeepEval scores the claims you give
  it. This tool separately runs a gap report — an exhaustive concept
  census used as ground truth for what no claim's citation ever touches —
  a question DeepEval's per-claim metrics don't ask at all.
- **Deployment.** DeepEval runs in your own environment or CI; this tool
  ships as actual infrastructure — Bicep-defined Azure Container Apps,
  per-tenant Key Vault/Postgres isolation, audit logging (see
  [infra/README.md](infra/README.md)) — the deployable service, not just
  the evaluation logic.

Reach for DeepEval to test your own LLM application's output during
development. Reach for this tool when you already have a finished set of
claims from an external tool or person and need an independent, auditable
judgment on whether a specific document actually supports them.

## Architecture

Every deployment is a **silo**: one Container App, one database, one Key
Vault, one Anthropic key, one storage share, per tenant. Nothing about a
tenant's data or spend is reachable through another tenant's credentials —
the only things two tenants share are the Container Apps *environment*
(compute/networking boundary, no data) and the Postgres *server* (each
tenant gets its own separate *database* on it, not a shared one).

Within one tenant's deployment, jobs and reports are further scoped to the
individual caller's identity (`owner_user_id` on the `Job` row) — real
per-person privacy when the tenant is configured for Entra ID (Azure AD)
auth, or one shared identity for everyone holding the same token under the
default shared-secret mode. Ontologies are the one thing genuinely shared
within a tenant: built once, immutable, listed for anyone on the tenant to
browse and reuse (`GET /api/ontologies`), never edited or deleted through
the API.

See the
**[deployment topology diagram](https://qa-au.github.io/claim-validator/architecture-diagrams.html)**
(source: [docs/architecture-diagrams.html](docs/architecture-diagrams.html))
for the topology diagram and the request-flow sequence, and
**[infra/README.md](infra/README.md)** for the actual Bicep-as-code
deployment: three templates (`pg-admin-identity.bicep` → `shared.bicep` →
`tenant.bicep`), deployed for real against a live Azure subscription this
project was built against — including every real deployment bug that
surfaced only at that stage (region availability, first-deploy RBAC
propagation races, an `arm64`-only image that took hours of misleading
"unauthorized" pull errors to trace back to `docker buildx --platform`,
and the Postgres AAD function signature) and the fixes for each, not just
the design.

Both the file share and the shared Postgres server have Storage
Analytics-equivalent audit logging wired up (`StorageFileLogs` and
`AzureDiagnostics`/`PostgreSQLLogs` in the shared `cv-logs` Log Analytics
workspace) — see infra/README.md's auditing sections for how to query
either one.

## Quickstart (local, no Azure)

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

```bash
.venv/bin/uvicorn claimvalidator.api:app --reload
```

All `/api/*` routes except `/api/ping` (and the auto-generated `/docs`
Swagger UI, which lives outside `/api/*` entirely) require a token —
`Authorization: Bearer <token>` or an `X-API-Token` header. If
`CLAIMVAL_API_TOKEN` is unset, one is generated and printed at startup
(see `phases/api_auth.py`). Set `CLAIMVAL_AZURE_TENANT_ID` instead to
require a real Entra ID access token per request — see
`claimvalidator/azure_auth.py` and docs/manual.html's tenancy section for
what that changes about job privacy.

Running against Docker instead of a bare venv: see the `Dockerfile` — it
documents the exact `docker buildx build --platform linux/amd64` command
up front, since a bare `docker build` on an Apple Silicon Mac produces an
`arm64` image that pushes and runs "successfully" everywhere except Azure
Container Apps, which needs `amd64` — a mistake that cost hours to
diagnose the first time (see infra/README.md's postmortem).

## Layout

- `phases/` — copied from the source repo (verbatim, except two additive
  edits noted in their own docstrings: `ontology_store.py`'s content-hash
  caching plus `created_by` attribution, `phase1_storage.py`'s one import
  redirected to the extracted `run_report_stem.py`).
- `claimvalidator/` — everything new: the retrieval adapter, the duck-typed
  shims that let a bare `id + text` claim satisfy `judge_entailment`'s and
  `check_requirement_shapes`'s existing contracts, the gap report, the
  async job/API layer (`api.py`, `jobs.py`, `pipeline.py`), Azure AD auth
  (`azure_auth.py`), and the ontology store's shared-list surface.
- `db/` — `models.py` (SQLAlchemy models, including `Job.owner_user_id`
  for per-caller scoping) and `database.py` (Azure AD Postgres auth,
  and `add_missing_columns` — a deliberately narrow auto-migration that
  adds a nullable column to an existing table without a full migration
  tool; see its own docstring for exactly how narrow).
- `infra/` — the three Bicep templates and their own README covering the
  full deployment sequence and every real bug found deploying them.
- `docs/` — the user-facing API manual, the architecture/flow diagrams,
  and the original Azure-readiness assessment this deployment was scoped
  from.
- `scripts/validate_claims.py` — the no-DB, no-HTTP path; also the script
  that proved the shims against a real model before anything else was
  built on top of them.

## Ontologies

Immutable and shared within a tenant once built — see docs/manual.html's
"Reusing ontologies" section for the two ways a caller gets one without
paying to rebuild it (automatic reuse by content hash, or explicit
`ontology_key` on `POST /api/validations`). There is deliberately no
`DELETE /api/ontologies/{key}` route: a shared, listed asset shouldn't be
destructible by any one authenticated caller.

## Tests

```bash
.venv/bin/pytest tests/ -q
```

159 tests. A mix of tests ported unmodified from the source repo
(`test_census.py`, `test_ontology_store.py`, `test_run_tracker.py` —
proving the reused logic still behaves as documented) and new ones for
this repo's own code, including the async job/API layer
(`test_api.py`, `test_jobs.py`, `test_run_validation_job.py`), Azure AD
auth (`test_azure_auth.py`, `test_database_azure_ad.py`), and the shims
that let a bare claim satisfy the reused judge/shape-check contracts
(`test_claim_shims.py`, `test_entailment_shim.py`,
`test_requirement_shapes_shim.py`).

`test_entailment_shim.py` is the automated version of this repo's central
early risk: does a bare claim's shim actually satisfy `judge_entailment`'s
duck-typing against a real call, not just look right in isolation. The
mocked version runs in CI; `scripts/validate_claims.py` against a real
model is what actually settled the question before this was trusted.

`test_run_validation_job.py` exists for a different reason: it's a
regression test for a real bug found live in production, not written
ahead of time. `run_validation_job` never had any test coverage at all,
which is exactly how it went unnoticed that it wasn't passing
`store_root`/`output_dir` through to the pipeline — every validation
submitted via `POST /api/validations` was silently building ontologies on
the container's own ephemeral disk instead of the persisted, mounted
share, so they worked fine until the container restarted and then simply
weren't there. Confirmed live against the real Azure deployment (built an
ontology, forced a fresh container revision, watched it survive) before
and after the fix.
