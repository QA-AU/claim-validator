# Azure API service readiness — punch list

What separates the current state of this project from a production API service on Azure. Ordered by risk, not by ease — a cheap fix that blocks real traffic outranks an expensive one that doesn't.

Grounded in what this project has actually measured about itself, not assumed: the pipeline logic (entailment judge, census, gap report, shape check) has been run for real, broken for real, and fixed for real across four documents and 120+ unit tests. The gaps below are specifically the *service layer* around that logic — concurrency, input hardening, secrets, RBAC, containerization — none of which has been built yet, because nothing has needed it yet.

## P0 — blockers, before any real (non-local) traffic

- [ ] **Input validation on the claims API.** Nothing currently rejects malformed input — the unquoted-CSV-comma bug that silently truncated a claim was caught by a verdict looking slightly off, not by any actual validation. A real API taking requests from external callers needs to reject bad input outright, not rely on someone noticing a strange result.
- [ ] **Replace the shared-secret token with real RBAC.** `phases/api_auth.py`'s own docstring says it's "a deliberate simplification for a single-user local workbench" — one flat token, no per-caller identity, no roles, and it's printed to logs at startup if unconfigured. Needs Azure AD (Entra ID) app registration with App Roles (e.g. `Validation.Submit`, `Validation.Read`, `Validation.Admin`), validated via JWT middleware in place of the current token check.
- [ ] **Load-test the background job model under real concurrency.** Never tested — "one process, one background task pool, unexercised under real load" is stated as an open item in the project's own paper, not a guess made here. Multiple simultaneous callers is the default condition for a real API, not an edge case.
- [ ] **Cost guardrails.** Nothing caps or throttles LLM spend per caller. A public-facing endpoint with no rate limit or spend cap can be run up arbitrarily by a misbehaving or malicious caller.
- [ ] **Drop Ollama from the production path, or hardened it out first.** This session measured it fragile under exactly the load a production API would apply: a 600-second timeout cliff at larger batch sizes (reproduced on two different local models, independent of parameter count), and Ollama Cloud's own GPU-time quota getting exhausted mid-project with no advance warning. Anthropic direct, or Claude via Azure AI Foundry (GA since June 2026, same tiers this project already defaults to), for the production path — keep Ollama as a local-dev-only option.

## P1 — needed before wider rollout

- [ ] **Containerize.** No Dockerfile exists yet, for any compute target (Container Apps, App Service, AKS).
- [ ] **Move the ontology cache and reports off local disk.** `OntologyStore` and the Excel report writer both do direct filesystem I/O against local paths — needs Azure Files mounted as a volume (least code change; Blob Storage would need an actual rewrite of that I/O layer).
- [ ] **Migrate off SQLite.** Lower-risk than it sounds: `db/database.py` already uses SQLAlchemy and its own docstring shows example `postgresql://` connection strings, so this is likely a `CLAIMVAL_DB_URL` change plus real testing, not a rewrite. Needs Azure Database for PostgreSQL (Flexible Server) provisioned and the migration actually verified, not assumed to just work.
- [ ] **Secrets into Key Vault.** `ANTHROPIC_API_KEY` is currently a plain environment variable. Needs Azure Key Vault, referenced via Managed Identity — no plaintext keys in app settings.
- [ ] **CI/CD pipeline.** Repo is already on GitHub; needs a GitHub Actions workflow to build, test, and deploy to Azure rather than manual pushes.

## P2 — real, but can follow the first deployment

- [ ] **Expand escalation test coverage.** Thinly proven so far — 5 credentialed attempts total across this whole project, 1 trigger, 1 confirmation. It fails safe by design (best-effort, never blocking a run), so this isn't a stability blocker, but it's not validated at any real volume either.
- [ ] **The gap report's two known, unfixed failure modes.** Chunk-coincidence at scale (RFC 6749) and unverifiable citations on small documents (the services agreement) — both documented honestly in the report itself already, not hidden, but neither has a fix attempted yet.
- [ ] **Real observability.** Application Insights or equivalent structured logging/metrics. The existing `RunTracker` DB audit trail is a complement to this, not a substitute — it records what a run did, not platform-level health/latency/error-rate signals.
- [ ] **Test two independent extractions of the same document disagreeing with each other.** A genuine open correctness question, not urgent for a first production release.

## What's already in good enough shape to build on

- Core judge/census/gap-report logic: real defects found and fixed through actual use this session (429 handling, a census spread that could fabricate a fake data point, an `AnthropicClient` bug that would have broken every real Anthropic+census call), 120+ unit tests, verified against real models across four documents.
- Async job model (FastAPI + `BackgroundTasks`, DB-backed status that survives a restart) already exists and works.
- DB layer is already Postgres-compatible in principle — the P1 item above is "prove it," not "build it."
- A working (if not yet role-based) auth gate already exists, fails closed by default on `/api/*`, and is a real foundation to extend rather than replace outright.
