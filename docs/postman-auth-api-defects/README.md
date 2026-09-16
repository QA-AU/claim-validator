# Example: SimpleAuthAPI — three single-defect variants

Reconstructed from a real validation run against this document
(`job_b8342503351f`, Postman test collection export) — see
[`reference-original.txt`](reference-original.txt), the exact ground
truth that run's 8 claims were judged against. That run is documented
in the assistant's own reply in this session: 4 `entails`, 2
`contradicts`, 2 `no_evidence`, all `3/3` agreement, no escalation.

Same 8 claims ([`claims.json`](claims.json), `C1`–`C8`), same document —
except each variant below changes exactly **one** fact, so resubmitting
the identical claim set against a variant isolates that one seeded
defect's effect on the verdict.

| Variant | What changed | Where | Claim it targets | Original verdict → new verdict |
|---|---|---|---|---|
| [`variant-1-rate-limit-mismatch.txt`](variant-1-rate-limit-mismatch.txt) | Rate limit lowered from **100** to **50** requests/minute per IP (`## Rate Limiting` and the `Critical Areas` bullet, kept consistent with each other) | `## Rate Limiting`, `## Critical Areas` | `C1` — "The rate limit is 100 requests per minute per IP address." | `entails` → `contradicts` |
| [`variant-2-token-never-expires.txt`](variant-2-token-never-expires.txt) | Every `expires_in` field and the `Token Expiration (3600 second TTL)` bullet removed; replaced with "Tokens do not expire once issued" | `## API Endpoints` (both response bodies), `## Data Models` (`AuthToken`), `## Critical Areas` | `C2` — "Authentication tokens never expire once issued." | `contradicts` → `entails` |
| [`variant-3-profile-404-removed.txt`](variant-3-profile-404-removed.txt) | The profile endpoint's `404: User not found` response code replaced with `200: Returns an empty profile object if the user is not found` | `### 3. GET /api/v1/user/profile` → `**Response Codes:**` | `C8` — "Getting the user profile returns a 404 status code if the user is not found." | `entails` → `contradicts` |

## Why these three specifically

Each defect was chosen to be **structurally different**, not just a
different number, so the three exercise different parts of the judge:

- **Variant 1** is a same-shape, wrong-value mismatch — the kind
  `numeric_threshold_check.py` is built to catch even if the judge's
  own free-text reading missed it (see `docs/known-fixes.md`'s
  "Numeric claims" section).
- **Variant 2** removes a fact entirely rather than changing it,
  flipping a `contradicts` to an `entails` — the harder direction,
  since there's no leftover wrong number for the judge to trip over on
  the way there.
- **Variant 3** swaps one response code's meaning for another
  plausible-sounding one (`404` → `200` on the same line shape), the
  kind of edit that reads fine at a glance and only fails on a careful
  line-by-line check against the claim.

## Run it

Same shape as `tools/openspec-adapter`'s examples: upload one of the
`.txt` files as the reference document, build (or reuse) an ontology
against it, submit `claims.json` unmodified, poll, read the verdicts.

```bash
# from the repo root — any of the three .txt files in place of <variant>
curl -s -X POST https://<tenant>.azurecontainerapps.io/api/documents \
  -H "Authorization: Bearer <token>" \
  -F "files=@docs/postman-auth-api-defects/<variant>.txt"

curl -s -X POST https://<tenant>.azurecontainerapps.io/api/validations \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d @<(jq -n --argjson claims "$(cat docs/postman-auth-api-defects/claims.json)" \
        --arg ontology_key "<key from the documents response>" \
        '{ontology_key: $ontology_key, claims: $claims}')
```
