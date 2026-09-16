# Example: SimpleAuthAPI v2 — API + UI requirements, 6 seeded errors

A hand-built demo bundle mixing API-related and UI-related
requirements in one document, with 6 deliberately seeded errors spread
across both categories and across verdict types — for a live demo where
the errors need to be guaranteed to show up, not left to chance the way
[`../llm-generated-requirements-demo/`](../llm-generated-requirements-demo/)
was (that one ran a real LLM against a real document and reported
whatever came back, including the possibility of nothing wrong at all).
This bundle is the opposite design point: same underlying document
shape, but the defects are placed on purpose so a demo can reliably
show them being caught.

| File | Role |
|---|---|
| [`simple-auth-api-v2.txt`](simple-auth-api-v2.txt) | the reference document — the original SimpleAuthAPI spec (`tests/postman_test_document.txt`, from the real `job_b8342503351f` run) with a new "Web Client UI" section added, describing the login page, session-expiry banner, and profile page. |
| [`claims-v2.json`](claims-v2.json) | 20 requirements: the original 8 API claims (`C1`–`C8`, unchanged, still valid since the API section wasn't touched), 6 new correct UI claims, and 6 seeded errors — interleaved, not grouped, so the pattern isn't obvious just from reading the file top to bottom. |

## Answer key — the 6 seeded errors

| Claim | Type | Requirement | Document actually says | Expected verdict |
|---|---|---|---|---|
| `C10` | API | Logout endpoint accepts `DELETE` | `### 4. POST /api/v1/logout` | `contradicts` |
| `C14` | API | API supports SSO via SAML | Never mentioned anywhere | `no_evidence` |
| `C18` | API | "Uses industry-standard security practices" | Bearer auth + HTTPS requirement stated; this exact phrase/claim never made | `mentions_only` |
| `C12` | UI | Session banner fires under **10 minutes** | "less than **5 minutes** remaining" | `contradicts` |
| `C16` | UI | Users can enable Face ID / Touch ID from the profile page | Never mentioned anywhere | `no_evidence` |
| `C20` | UI | Successful login redirects to the **profile page** | "redirects the browser to **/dashboard**" | `contradicts` |

The other 14 claims (`C1`–`C9`, `C11`, `C13`, `C15`, `C17`, `C19`) are
faithfully grounded in the document and should come back `entails`.

## Why these three shapes per category

- **`contradicts`** (`C10`, `C12`, `C20`) — a stated fact directly
  swapped for a different, equally concrete one (method, number,
  destination). The easiest kind for the judge to catch, and the most
  common real-world documentation error.
- **`no_evidence`** (`C14`, `C16`) — a plausible-sounding feature
  invented wholesale, with nothing in the document to check it
  against. Tests that the judge doesn't get talked into inventing
  support that isn't there.
- **`mentions_only`** (`C18`) — a vague, unfalsifiable claim on a
  topic the document does discuss (auth, HTTPS) without ever stating
  the specific thing claimed. The hardest of the three to write well
  and the one most often skipped in a demo — included on purpose.

## Run it

Same shape as
[`../postman-auth-api-defects/`](../postman-auth-api-defects/): upload
`simple-auth-api-v2.txt` as the reference document, build an ontology
against it, submit `claims-v2.json` unmodified, poll, read the
verdicts.

```bash
# from the repo root
curl -s -X POST https://<tenant>.azurecontainerapps.io/api/documents \
  -H "Authorization: Bearer <token>" \
  -F "file=@docs/simple-auth-api-v2-demo/simple-auth-api-v2.txt"

curl -s -X POST https://<tenant>.azurecontainerapps.io/api/ontologies \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"files": ["<path from the upload response>"]}'

curl -s -X POST https://<tenant>.azurecontainerapps.io/api/validations \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d "{\"document\": {\"files\": []}, \"claims\": $(cat docs/simple-auth-api-v2-demo/claims-v2.json), \"ontology_key\": \"<key from the ontology response>\"}"
```

Note this builds a **new** ontology — `simple-auth-api-v2.txt` isn't
byte-identical to the original spec (the UI section is new), so the
existing cached `simple-auth-api-4f17` ontology doesn't apply here.
