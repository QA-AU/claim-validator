# Presentation Q&A — Claim Validator

Anticipated questions for a presentation of Claim Validator, grouped by audience: QA/test, AI engineer, executive, and deep technical/implementation. Every answer below is grounded in what's actually shipped and live-verified against the running Azure service — nothing here is aspirational.

See also: [executive-overview.md](executive-overview.md), [architecture-diagrams.html](architecture-diagrams.html).

---

## From a QA / test perspective

**Q: What exactly counts as a "verdict" — is this a pass/fail check?**
No single pass/fail. Every claim gets one of four verdicts: `entails` (supported), `contradicts` (the document says otherwise), `mentions_only` (an on-topic passage exists but doesn't address the specific claim), or `no_evidence` (nothing in the document supports or contradicts it). Collapsing those into one score is exactly what this tool refuses to do — a fabrication and an unaddressed claim are different defects and need different follow-up.

**Q: How do you know the verdict itself is reliable, not just confidently wrong?**
Each claim is judged three separate times and reported by majority vote; the agreement ratio (e.g. `2/3`) ships with the verdict, not thrown away. Low agreement is itself a signal worth reviewing even when the majority verdict looks fine.

**Q: Has anyone scored it against a known answer key, not just eyeballed the output?**
Yes — most recently against a real [OpenSpec](https://github.com/Fission-AI/OpenSpec) capability spec (the `openspec list` command spec, from spec-driven-development tooling). 16 claims were hand-written with a target verdict fixed *before* the run — 7 to entail, 5 to contradict, 2 `mentions_only`, 2 `no_evidence` — then scored against that key. Result: **14/16 exact**. Both misses were boundary calls (`mentions_only` vs `contradicts`; `no_evidence` vs `mentions_only`, with the judge itself splitting 2/3 on that one), not wrong-direction errors — nothing false was called `entails`, and no real contradiction was missed. Full breakdown is in the paper's Evidence section.

**Q: That compound-claim issue (#3) — how often does it actually bite?**
Not always. In the OpenSpec test above, two claims were built specifically to trigger it: one bundling four separately-true assertions, one with a single false clause set between two true ones. Both were judged correctly 3/3 — the false-clause one with that clause named in the judge's reason. The failure mode is real but conditional: it depends on retrieval surfacing every part of the claim, not on the claim being compound per se.

**Q: If it flags something, how does a QA engineer actually verify it without re-reading the whole document?**
That's what `cited_passages` (shipped and live-tested) is for — the actual sentences the judge read, in citation order, right next to the verdict, both in the JSON and the Excel report's "Cited passages" column. No more starting from a bare chunk index.

**Q: What if the ontology spans more than one source document — how do I know which file a citation came from?**
`cited_sources` (also shipped and live-tested) names the source file per passage, but only when a claim's citations actually span more than one document — a single-document validation renders exactly as it did before, no extra noise.

**Q: Does a well-formed claim guarantee it gets checked?**
No — there's a separate, free, deterministic shape check before any model call: does the claim even state something checkable (a subject, at least one criterion)? A claim can be entirely true and still fail this check because it says nothing checkable. Failing claims are flagged, never silently dropped.

**Q: What are the known gaps right now — anything QA should specifically watch for?**
Two, both filed as GitHub issues and deliberately left open rather than papered over: a gap-report fuzzy-matching blind spot (#2) and a compound-claim retrieval miss (#3) — claims that bundle multiple assertions can retrieve evidence for only part of themselves. Both need a real design pass, not a quick patch.

**Q: Are there limits on file size or number of claims I should plan tests around?**
25MB per upload (`MAX_UPLOAD_BYTES`), enforced. No cap on the number of claims in a batch.

**Q: What happens if I resubmit the same document?**
It's caught by content hash and reused rather than re-indexed — confirmed live, including the exact `ontology_reused: true` behavior on identical resubmits.

---

## From an AI engineer perspective

**Q: What's the actual pipeline — is this RAG, or a single big prompt?**
RAG, deliberately: the document is chunked and indexed once per unique content hash, an ontology (concept types) is extracted per document by one LLM pass, then each claim is *retrieved against* before it's ever shown to the judge. Claims never touch the judge directly — every verdict is anchored to a specific retrieved passage, not a bare model opinion over the whole document.

**Q: Why not just use DeepEval or another off-the-shelf faithfulness metric?**
DeepEval answers the right top-level question (is this grounded), but a Faithfulness score alone doesn't tell you *which* claim is the problem or *why* — that's the actual gap this was built to close. This isn't a replacement metric; it's claim-level, citation-anchored verdicts you can act on individually.

**Q: What models does it run on, and is it locked to one?**
Anthropic Claude models, selected via a model-tier config rather than hardcoded IDs, so the judge tier can be swapped without touching pipeline code. A model swap was one of the things live-tested this project.

**Q: How expensive is a validation run — are you calling the LLM per claim, three times, plus retrieval?**
Retrieval and the ontology extraction are one-time, cached by content hash. Judging is 3 calls per claim (for the majority-vote design). Cited-passage and cited-source surfacing added *zero* new LLM calls — that data was already loaded in memory at verdict-construction time; it was just never returned before.

**Q: Give me a real cost figure for a typical run.**
A 16-claim validation against a ~3.7 KB document, judged by Haiku (the default cheap tier): 22 LLM calls total (18 entailment + 4 gap-report), ~77K tokens (70.7K in / 6.0K out). The ontology build for that document was a separate one-time call. That's the whole job — from the OpenSpec answer-key test.

**Q: Is the ontology extraction generic, or does it assume a domain (e.g. requirements)?**
Generic — concept types are discovered per-document, not assumed in advance. It's been run against software requirements, legal text, a compliance standard (RFC 2119), and plain public-health claims, same pipeline, no reconfiguration.

**Q: Can this be called by an agent instead of a human, as part of an autonomous pipeline?**
Yes, by design — every endpoint is plain REST (`POST /api/documents`, `POST /api/ontologies`, `POST /api/validations`, `GET .../report`), no session/UI dependency. An agent can validate its own generated claims against a source document the same way a human tester does, polling for job completion and reading the JSON verdict.

**Q: Does it support multiple related source documents as one knowledge base?**
Yes — content hashing already treats a set of files as one "document set," merging all their chunks into one retrieval index and discovering one unified concept schema across all of them. Confirmed live with two unrelated documents (an API spec + poker hand rankings) combined into one 7-concept-type ontology, with citations correctly attributing each claim back to its actual source file.

**Q: What's deliberately *not* built, and why?**
No free-text "ask the document a question" endpoint — considered and explicitly rejected. The design goal is a human proofreading the actual source passage, not trusting a second AI-generated answer layered on top of the first claim. Also no field for claim provenance (which model/prompt produced it) or declared intent — the judge is deliberately blind to who wrote a claim and why, so framing can't talk it into a more lenient verdict.

**Q: What's still open / not production-hardened?**
The two filed issues (#2 fuzzy gap-matching, #3 compound-claim retrieval) — both real, both understood, both intentionally deferred pending proper design rather than a rushed fix.

---

## From an executive perspective

**Q: What business problem does this actually solve?**
LLMs get claims about a document wrong a small but nonzero fraction of the time — roughly 5% by informal experience. That's not high enough to ignore and not low enough to trust blindly. The alternative to this tool is manually re-reading everything the LLM produced, which erases the reason for using the LLM at all. This gives a pinpointed, citation-backed answer instead of a re-read-everything workaround.

**Q: How is this different from generic "AI evaluation" tools already on the market?**
Most give you a single groundedness score. A score tells you something is probably wrong; it doesn't tell you what, or where in the document to look. This gives per-claim verdicts, each pointing at the exact sentence it's based on — the difference between a red light and an actual diagnosis.

**Q: Is customer/document data safe? What's the security model?**
Every deployment is a fully isolated tenant — its own compute, database, and secret store, reachable only through its own credentials. No passwords anywhere in the system: every internal connection uses Azure managed identity and scoped role assignments, not stored secrets. Two tenants share only compute infrastructure and an activity log that records *what* happened, never document content.

**Q: Is this live today, or a prototype?**
Live and running on Azure, two active tenant deployments, verified continuously against real API calls and real downloaded reports — every feature claimed in the accompanying paper was tested against the running service, not just designed.

**Q: Who can use it — does it require engineering staff to operate?**
It's callable by a human tester through a simple REST API (documented with Postman-style examples) or by another AI system automatically validating its own output. No UI is required, though one could be layered on top later.

**Q: What are the known limitations we should be aware of before we rely on this broadly?**
Two specific, documented gaps — a fuzzy-matching blind spot in the coverage/gap report, and reduced accuracy on claims that bundle multiple assertions together — both tracked as open engineering issues, not hidden. Everything else in the paper's "Boundaries" section is a deliberate scope decision (e.g., it validates against a supplied source document; it doesn't independently fact-check against the world).

**Q: What's the cost driver if we scale this up?**
Primarily LLM API calls for judging (3 per claim) and one-time ontology extraction per unique document set — both cached aggressively by content hash so repeat submissions of the same material cost nothing extra.

---

## Deep technical / implementation

**Q: Is retrieval doing real embedding search, or something simpler?**
It's the RAG indexer already built for the underlying research pipeline (`phase1_rag_indexer.py`) — chunks get indexed and searched per claim via `retrieve_for_claim`/`searcher_for`, and each chunk tracks its own source file via a bounds-checked `source_of(chunk_index)` method. That method existed and was tested well before this session's `cited_sources` feature started calling it from the API layer — the retrieval machinery was deliberately over-built for "retrieve before asking," so surfacing citations was data plumbing, not new capability.

**Q: How exactly does content-hash caching work for multiple documents — is file order significant?**
No — `content_hash()` hashes the bytes of every file in the set together in an order-independent way, so the same set of files resubmitted in a different order still hits the cache and skips re-indexing. This was confirmed live: resubmitting the same document under a different name, and reordering a multi-file `files` list, both correctly reused the existing ontology (`ontology_reused: true`).

**Q: What actually happens on a 3-way judge split with no clear majority?**
The result records `judged: True` but `decided: False` — surfaced in the report as "undecided," distinct from a claim that was never judged at all (e.g., because it failed the shape check first). It's not silently coerced into whichever verdict happened to come first.

**Q: How clean is the line between `no_evidence` and `mentions_only` in practice?**
It's the fuzziest of the four boundaries, and the agreement ratio tells you when a verdict is sitting on it. `no_evidence` means retrieval found nothing on-topic at all; `mentions_only` means it found something adjacent that just doesn't address the specific claim — and "adjacent" is a judgment call. In the OpenSpec test, a claim about a CLI flag the document never mentions was labelled `no_evidence` in the answer key; the tool returned `mentions_only` at 2/3 agreement, anchoring on the document's "output format" requirement as an on-topic passage that stays silent on the flag. Defensible either way — and the split `2/3` is the visible signal that it's a borderline call, not a confident one.

**Q: Why is `require_subject` forced to `False` in the shape-check rules, even when a shape profile is inferred?**
Because the bare-claim shim (`_ShapeClaim`) has no real subject/criteria field structure — those checks were designed for structured software requirements (`GET /projects`-style), not free-text `id + text` claims. Clamping happens regardless of what the LLM proposes when inferring a shape profile: `require_subject` is always forced `False`, `id_pattern` is always dropped, and `"expected_behavior"` is force-injected into `require_any_of`. A pattern the model proposes without also setting `wants_subject_check` is logged as a note but explicitly not stored — a real defect this design point closes: a proposed pattern with no signal that the document even has distinguishable subjects isn't safe to silently apply as a filter.

**Q: How is a bad `subject_pattern`/`id_pattern` regex handled at evaluation time — does a malformed pattern crash a run?**
No — `_evaluate()`'s two `re.search()` calls are wrapped in `try/except (re.error, TypeError)`; a broken pattern degrades to a `Violation` result instead of raising an unhandled exception mid-run. This was a real, pre-existing gap (not just theoretical) found and hardened this session.

**Q: Where does the request-level shape override sit relative to an ontology's own inferred profile — which wins?**
`resolve_shape_rules(overrides, base=None)` merges a per-request override *on top of* the ontology's own inferred base profile — request-level always wins where both specify something, but an ontology's inferred profile still applies where the request is silent. The report's `quality` block exposes both `shape_profile_source` (`default`/`llm`/`llm_failed`) and `shape_profile_overridden_by_request` so it's auditable which layer actually produced the effective rule.

**Q: Are shape-rule overrides validated, or can a caller pass an arbitrary regex/dict?**
Validated — `ShapeRuleOverrides` is a real Pydantic model (regex-checked `subject_pattern`/`id_pattern`, length-capped lists), replacing what used to be an unvalidated `Dict[str, Any]` on `ValidationOptions.shape_rules`.

**Q: How is job/report privacy enforced — can one caller see another's validation jobs?**
Every `Job` row carries `owner_user_id`. Under the default shared-bearer-token mode, everyone on that token effectively shares access; under the optional Entra ID (Azure AD) auth mode, isolation is per real person, not just per tenant.

**Q: How does the async job model work — polling only, or are there webhooks?**
Both. `GET /api/validations/{job_id}` polls for JSON status; on completion the job also fires a webhook via `webhooks.py::deliver()`. The Excel report itself is a separate binary-download endpoint (`.../report`), not embedded in the polling response — a real point of confusion during manual Postman testing, since the polling endpoint correctly returns `application/json` and doesn't turn into the report on its own.

**Q: Where does the actual LLM cost land in the pipeline — how many calls per run?**
Per unique document set: one call for ontology/concept-type extraction, plus one more if `infer_shape_profile` is opted in. Per claim: three judge calls (majority vote). Cited-passage and cited-source surfacing added zero new calls — that data was already resident in memory when the verdict object was built.

**Q: Is there a formula-injection risk in the Excel report, given cell values come from LLM output?**
Guarded — a report cell value starting with a formula-trigger character (e.g. a bare `-`) gets defused by prefixing with `'` (so `"-"` becomes `"'-"` in the actual cell), applied consistently across every text column including the newer `cited_passages` and `source_ref` columns.

**Q: How do you deploy a plain code change (no infra change) to the live tenants?**
`az acr build --registry <acr> --platform linux/amd64 -t claim-validator:latest .` to rebuild the image (explicitly platform-pinned — an earlier real bug was an `arm64` image built on Apple Silicon that pulled "successfully" everywhere except Azure Container Apps), then `az containerapp update --revision-suffix <unique> --image ...:latest`, then poll `/api/ping` for `200` and confirm the new revision shows `Running`. Documented in `infra/README.md` and repeated for real multiple times this project with no surprises.

**Q: How is a database credential ever used, given there's no Postgres admin password stored anywhere?**
A single shared managed identity (`pg-admin-identity`) exists purely to *grant* each tenant's own Container App identity its Postgres AAD role at setup time — it's never used at runtime by either tenant afterward. No admin password exists in Key Vault, in a Container App's config, or in the Bicep templates.

**Q: How is backward compatibility maintained when new fields get added to core objects like `ClaimResult` or ontology metadata?**
Every addition is a defaulted field (`field(default_factory=list)`, plain `= False`/`None`), and metadata loading (`OntologyMeta.from_dict`) uses `.get()` rather than direct key access — an older stored ontology missing the newer keys still loads correctly instead of raising. New dataclass fields are also placed after existing non-default fields per Python's field-ordering rule, with dict-key order in `to_dict()` chosen independently for readability rather than mirroring declaration order.

**Q: What's the actual test discipline — do you deploy without running the suite?**
No — every feature this project went through the same sequence: unit tests added and passing locally (229 at last count), deployed to the live tenant, then verified with a real API call and a real downloaded report before being considered done — not just designed or unit-tested in isolation.
