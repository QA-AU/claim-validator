# Validating OpenSpec output with Claim Validator

Using Claim Validator to check whether the requirements an
[OpenSpec](https://github.com/Fission-AI/OpenSpec) run generated are
actually grounded in the source document they were written from.

This is **review assistance**, not a gate. It tells a human reviewer which
generated requirements to look at hardest; it does not approve a spec.

---

## What OpenSpec is, and where the risk is

OpenSpec is a spec-driven-development layer for AI coding assistants. It
is a convention + prompting layer, **not a model** — the folder structure,
the delta-spec format, and the slash commands are OpenSpec; the actual
`### Requirement:` and `#### Scenario:` text is written by whatever
assistant you run it through (Claude, Cursor's model, …). So the generated
requirements are LLM output, shaped by OpenSpec's format.

The intended OpenSpec loop has a human gate in the middle:

1. `openspec init` — scaffold the `openspec/` directory.
2. **Propose** — describe the change; the assistant generates a change
   folder (`proposal.md`, `specs/<cap>/spec.md`, `design.md`, `tasks.md`).
3. **Review and edit** — you read the proposal, catch what is wrong,
   missing, or over-reaching, and fix it.
4. **Approve** — the assistant implements against `tasks.md`.
5. `openspec archive` — the change merges into `openspec/specs/` as the
   new living spec.

`openspec validate` checks **structure only** — required headers, scenario
format, whether requirement bodies use `SHALL`/`MUST`. A requirement that
cleanly misreads the source brief passes `validate` fine. The only
safeguard against that is the manual review in step 3 — one person reading
N generated requirements against the brief, unaided.

Claim Validator makes that review targeted instead of exhaustive.

---

## The pipeline

```
OpenSpec propose ──► adapter ──► claims.json ──► runner ──► Claim Validator ──► report
   (assistant)      (this repo)                 (your ~50    (existing API)
                                                 line glue)
                                                                    │
                        reviewer reads the flagged requirements ◄────┘
                        (contradicts first, then mentions_only)
```

Three independent pieces:

| Piece | What it is | Where it lives |
|---|---|---|
| **Claim Validator** | unchanged — the neutral judge; takes `{id, text, source_ref}` + a reference document, knows nothing about OpenSpec | this repo / its Azure deployment |
| **openspec-adapter** | deterministic, LLM-free transform: OpenSpec `spec.md` → claim list | [`tools/openspec-adapter/`](../tools/openspec-adapter/README.md) — a dev tool, not in the deployed image |
| **runner** | a short HTTP client that uploads the source doc, submits the claims, polls, downloads the report | you write it once; it lives wherever your specs repo is |

The adapter never calls Claim Validator. The runner is just a client of
Claim Validator's **existing** API (`POST /api/documents`,
`POST /api/ontologies`, `POST /api/validations`,
`GET /api/validations/{id}/report`) — nothing new is exposed for this.

---

## What maps to what

| Claim Validator input | In this workflow |
|---|---|
| **Reference document** (the RAG / ground-truth source) | the document you fed OpenSpec to work from — the brief, PRD, design note, or existing README |
| **Claims** (the output under test) | the generated OpenSpec requirements + scenarios, run through the adapter |

The check runs **claims → against → reference**: "is each generated spec
statement backed by the source document."

### Three things that decide whether the result is meaningful

1. **The source has to be substantial.** If OpenSpec produced 40
   requirements from a one-paragraph brief, most will legitimately come
   back `no_evidence` — the model elaborated past the source, which is
   partly what a spec is *for*. The stronger the source doc, the sharper
   the signal.
2. **Delta / `MODIFIED` specs need two files as the reference.** A
   modified requirement is grounded partly in the *existing* spec it
   changes. So the reference document set = **source brief + the
   pre-change `openspec/specs/<cap>/spec.md`**. Claim Validator takes both
   as one multi-document ontology (the content-hashed "document set");
   upload both, build one ontology. Give it only the brief and every
   modified requirement reads as spurious `no_evidence`.
3. **Some generated requirements are about OpenSpec itself** ("the spec
   SHALL follow OpenSpec conventions"). Those are noise here — expect a
   tail of `no_evidence` that is not the model getting the domain wrong.

---

## Steps

1. **Generate** the specs with OpenSpec as normal.

2. **Normalize** with the adapter (see its
   [README](../tools/openspec-adapter/README.md) for all options):

   ```bash
   python tools/openspec-adapter/openspec_to_claims.py \
       openspec/changes/<change>/specs/ \
       --map claims.map.json -o claims.json --stats
   ```

   Default granularity is `assertion` — one claim per `THEN`/`AND` bullet,
   plus one per requirement's `SHALL` statement. Keep it there: Claim
   Validator's compound-claim retrieval gap (issue #3) is triggered by
   bundling several checkable statements into one claim.

3. **Run** the validation with your runner script (or by hand / Postman):
   upload the source brief (and the pre-change spec, for a delta),
   `POST /api/ontologies` to build one ontology across them,
   `POST /api/validations` with `claims.json`, poll
   `GET /api/validations/{job_id}` to `done`, then
   `GET /api/validations/{job_id}/report` for the Excel.

4. **Triage** the report (below).

---

## Reading the results

| Verdict on a generated requirement | What it usually means | Action |
|---|---|---|
| `contradicts` | the requirement asserts something the source rules out — a real grounding failure | **look first.** This is the signal the workflow exists for. |
| `mentions_only` | the source touches the topic but not this specific claim | check the ones that *should* have had direct support; the rest are often the spec being more specific than the brief |
| `no_evidence` | the source says nothing on this at all | the spec added intent here — is that intent wanted and correct? Not a defect by itself. |
| `entails` | backed by the source | nothing to do |

The `agreement` ratio (e.g. `2/3`) rides along with each verdict — a split
is the tool flagging its own borderline call, worth a glance regardless of
which way it landed.

**Reverse direction, for free:** the gap report (concept census) in the
same job lists which concepts from the source brief *no* generated
requirement covered — i.e. what OpenSpec missed. Weaker signal (open issue
#2), but it is there without extra work.

---

## Repeating it

Cheap enough to run on every proposal (a 16-claim run against a ~4 KB
document was ~22 LLM calls / ~77K tokens on the default Haiku tier). Three
ways you would repeat:

| You change… | What you learn | Ontology |
|---|---|---|
| nothing (same specs, same brief) | verdict stability — a claim that flips between runs is borderline | cached, not rebuilt |
| the generation (new model, tweaked prompt, revised brief) | whether that change improved grounding | rebuilt only if the brief changed |
| the specs (reviewer edited them) | whether the edits resolved the flagged contradictions | cached if brief unchanged |

---

## Should this be its own API? Is it an LLM task?

**The adapter is a non-LLM task**, on purpose. Every step in it is a
deterministic rule — parse the OpenSpec markdown grammar, split each
requirement into atomic assertions, phrase each as one sentence from a
fixed template, emit JSON. No model is called. That matters: the generated
specs are the thing under test, so the step that prepares them for judging
must not itself be a model that could quietly correct or distort them.
(Using an LLM chat to do the conversion for a one-off, with your own
review of the output, is fine; as the standing pipeline for a repeatable
measurement, it puts a second model in the measurement path.)

**It should not be its own API.** It is a pure, fast, stateless function —
markdown in, JSON out, in milliseconds. Wrapping that in a service adds a
deployment, an auth surface, a network hop, and a version consumers are
coupled to, for no benefit. Ship it as the CLI / small library it is and
pin a version. Contrast Claim Validator itself, which *is* appropriately
an API: LLM-backed, slow, async, stateful (jobs, ontologies), tenant-
scoped auth. The adapter has none of those properties.

An API is only justified if you build a hosted "paste an OpenSpec spec + a
source doc, get a report" product for non-technical users — and even then
the adapter is a *library inside that orchestrator*, not the service; the
orchestrator calls the adapter as code and Claim Validator as its existing
API.
