# Example: LLM-generated requirements — JSONPlaceholder API

An end-to-end run of the full intended workflow: a real, small, public
source document → a cheaper/simpler LLM (`claude-haiku-4-5`) writes a
requirements document from it → the requirements are submitted to
Claim Validator's live API, unmodified → real verdicts come back.
Nothing here was hand-seeded — every result below is what actually
happened when this was run for real, on `usera-claimval`.

| File | Role |
|---|---|
| [`source-jsonplaceholder-api.txt`](source-jsonplaceholder-api.txt) | the reference document — real content from [jsonplaceholder.typicode.com](https://jsonplaceholder.typicode.com/) and its [guide page](https://jsonplaceholder.typicode.com/guide/), compiled into plain text. This is ground truth. |
| [`generated-requirements.md`](generated-requirements.md) | the prompt used and the model's raw output — 19 requirements, unedited. |
| [`claims.json`](claims.json) | the same 19 requirements as `{id, text}`, submitted as-is. |
| [`validation-result.json`](validation-result.json) | the full, real API response from the live run (`job_9bfaeb6d8641`, ontology `doc-0fc2ccd7-c6fc`). |

## What actually happened

```
19 claims submitted, 19 judged, 3/3 agreement on every one
  17 entails
   2 mentions_only
   0 contradicts
   0 no_evidence
```

Genuinely clean — on a small, unambiguous real document, a capable
model's first-draft summary turned out almost entirely faithful. That
by itself is a real, honest finding, not a null result: it says
something about how rarely a competent model invents facts when the
source is short and explicit, which is exactly the kind of claim this
project's own paper is careful never to just assert without measuring.

## The one genuinely interesting finding

Four requirements have the *identical* evidentiary shape: the source
document lists a nested route (`/posts/1/comments`, `/albums/1/photos`,
`/users/1/albums`, `/users/1/todos`, `/users/1/posts`) without ever
spelling out in words what each one returns — the reader is expected
to infer it from the pattern ("posts have many comments", "one level
of nested route is available").

The judge did not treat these consistently:

| Requirement | Verdict | Judge's reasoning |
|---|---|---|
| `R4` — `/posts/1/comments` | `mentions_only` | "listed as an available route... but do not explicitly state what this endpoint returns" |
| `R15` — `/albums/1/photos` | `mentions_only` | "do not explicitly specify what GET /albums/1/photos re[turns]" |
| `R16` — `/users/1/albums` | `entails` | "establish the pattern that nested routes return all resources belonging to a user" |
| `R17` — `/users/1/todos` | `entails` | same pattern-inference reasoning |
| `R18` — `/users/1/posts` | `entails` | same pattern-inference reasoning |

Same document, same claim shape, same underlying evidence — two
verdicts. `R4`/`R15` got the strict reading (the sentence isn't
literally there); `R16`–`R18` got the lenient one (the pattern is
"established" so it counts). This isn't the judge being wrong on any
one of the five in isolation — each individual reasoning is
defensible — it's that the same evidentiary bar was applied
inconsistently across five structurally identical claims *within one
run*. Related to, but distinct from, the run-to-run variance already
tracked in
[issue #7](https://github.com/QA-AU/claim-validator/issues/7): that
issue is about the same claim landing differently across repeated
runs; this is five different-but-equivalent claims landing differently
within the same run. Worth its own issue if this project wants to
chase it — not filed yet, since one run isn't enough to say whether
it reproduces.

## Run it yourself

```bash
# 1. upload the source document
curl -s -X POST https://<tenant>.azurecontainerapps.io/api/documents \
  -H "Authorization: Bearer <token>" \
  -F "file=@docs/llm-generated-requirements-demo/source-jsonplaceholder-api.txt"

# 2. build the ontology (use the path returned above)
curl -s -X POST https://<tenant>.azurecontainerapps.io/api/ontologies \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"files": ["<path from step 1>"], "background_description": "Public REST API documentation for a fake-data testing API."}'

# 3. submit the claims (use the ontology key returned above)
curl -s -X POST https://<tenant>.azurecontainerapps.io/api/validations \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d "{\"document\": {\"files\": []}, \"claims\": $(cat docs/llm-generated-requirements-demo/claims.json), \"ontology_key\": \"<key from step 2>\"}"

# 4. poll for the result
curl -s https://<tenant>.azurecontainerapps.io/api/validations/<job_id> \
  -H "Authorization: Bearer <token>"
```

To regenerate the requirements document itself (step 0, before any of
the above), see the exact prompt in
[`generated-requirements.md`](generated-requirements.md) — any
Anthropic model works; this run used `claude-haiku-4-5` deliberately,
as the "simpler/cheaper" model in the pair.
