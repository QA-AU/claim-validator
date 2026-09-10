# Test fixtures

- **`cli-list/spec.md`** — a real OpenSpec capability spec, copied verbatim
  from [Fission-AI/OpenSpec](https://github.com/Fission-AI/OpenSpec)
  (`openspec/specs/cli-list/spec.md`), MIT licensed, © 2024 OpenSpec
  Contributors. Used unmodified as a parser fixture.
- **`sample-change/spec.md`** — a synthetic delta spec written for this
  repo, exercising `## ADDED / MODIFIED / REMOVED Requirements`.
- **`*.claims.json`** — committed golden output. Regenerate with:
  ```bash
  python openspec_to_claims.py tests/fixtures/cli-list/spec.md -o tests/fixtures/cli-list.claims.json
  python openspec_to_claims.py tests/fixtures/sample-change/spec.md --include-removed -o tests/fixtures/sample-change.claims.json
  ```
  A diff here means the parser's output changed — intended or not.
