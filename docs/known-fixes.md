# Known fixes and edge cases

A running inventory of specific defects and edge cases this project has
found live and fixed — pulled from the codebase's own `# found live`
comment trail, not from memory, so it stays traceable to the actual
code. Update this when a new one is found and fixed; it's a companion
to `docs/test-results-and-observations.md` (the benchmark numbers) and
`docs/judge-flow.md` (the mechanism these fixes sit inside).

## Claim judging — verdict logic

**Entailment judge prompt** (`phases/entailment.py`)

- Numeric threshold comparisons judged literally instead of by boundary
  rule — three rounds of prompt engineering before the deterministic
  checker (below) took over the cases prompting alone couldn't close.
- `mentions_only`/`no_evidence` boundary — asymmetric definitions biased
  the judge toward `mentions_only` on any topical overlap at all;
  rewritten so both verdicts hinge on the same test: does the passage
  name the *specific* subject, and does it confirm the specific detail.
- Quantifier weakening ("all" → "some"/"most") scored inconsistently
  ([#14](https://github.com/QA-AU/claim-validator/issues/14)) — the
  judge already noticed the narrowing in its own stated reasoning but
  had no instruction on which way to score it once noticed; now
  explicit.
- 3-run majority vote + escalation to a stronger model for doubtful
  verdicts (no majority, or a bare 2/3 `contradicts`) — the judge's own
  measured ~20–25% single-pass variance made this necessary, not
  optional.

## Numeric claims — deterministic overrides

`claimvalidator/numeric_threshold_check.py` — three narrowly-recognized
shapes, everything else deliberately left to the judge:

- **Shape 1** — a claim's value against a stated threshold rule
  ([#4](https://github.com/QA-AU/claim-validator/issues/4)/[#6](https://github.com/QA-AU/claim-validator/issues/6)).
- **Shape 2** — a restated single bound, no outcome word on either side
  ([#15](https://github.com/QA-AU/claim-validator/issues/15)) — the
  judge's own reasoning reached the right answer, then stated the
  opposite verdict.
- **Shape 3** — a restated two-number range, "between X and Y" / "from
  X to Y" only ([#8](https://github.com/QA-AU/claim-validator/issues/8))
  — a bare "X-Y" hyphen form deliberately unhandled, genuinely
  ambiguous with a section, date, or version reference.

Edge cases found hardening these:

- A claim restating the *document's own* threshold phrase — not
  reporting an instance value — was producing a false `contradicts`
  ([#6](https://github.com/QA-AU/claim-validator/issues/6)).
- Markdown bold markers sitting directly between an operator and its
  number broke the adjacency match.
- A markdown heading with no terminal punctuation merged into the next
  sentence, tripling the apparent outcome-word count.
- `"passed"` removed from the outcome-word vocabulary entirely —
  genuinely ambiguous between "a check passed" and "a flag was passed,"
  with no way to disambiguate from the word alone.
- A claim range that's a strict subset of a wider stated range abstains
  rather than being scored either way — a narrower true statement isn't
  a contradiction.
- The match threshold for "is this the same fact" (shape 3) needed
  recalibrating against a real claim/passage paraphrase gap — a
  threshold copied from shape 2 rejected a genuine match found live
  against real API-retrieved passage text.

## Retrieval

`claimvalidator/claim_retrieval.py`

- Compound claims (a comma/semicolon before a coordinating "and")
  widened into per-clause queries, unioned and deduped
  ([#3](https://github.com/QA-AU/claim-validator/issues/3)) — a single
  shared query risked the more prominent fact's vocabulary crowding out
  the other's.

## Coverage / gap report

- Fuzzy name matching's low-overlap blind spot — an LLM-judged
  reconciliation fallback added for names the free matcher already gave
  up on ([#2](https://github.com/QA-AU/claim-validator/issues/2)).
- A census with failed batches isn't a census — 17 of 19 batches
  failing once still reported a range as though it were a real
  measurement; now reported as "not measured" instead.
- A rate-limit storm on the census could fabricate a spurious low end
  in a reported spread; excluded from the range rather than counted as
  zero.
- A concept's census spread genuinely `[0, N]` (seen in some runs, not
  others) caused a `ZeroDivisionError` in the gap report's reconciled
  capture-range math.

## Shape check

`phases/requirement_shapes.py`, `phases/shape_profile_inference.py`

- An OpenAPI tag filed as if it were an endpoint — a present-but-wrong
  "subject" needed pattern-matching, not just presence-checking.
- A model-proposed shape rule that gets silently forced off (e.g.
  `require_subject`) or dropped (invalid regex) now always logs why —
  an earlier version silently dropped a valid pattern the model
  offered, with no record that it happened.

## Multi-provider client robustness

- Ollama's cloud-hosted proxy 429s under back-to-back census batches —
  5 retries with exponential backoff, honoring a `Retry-After` header
  when the server sends one.
- Ollama's cloud-hosted models (e.g. `gpt-oss:120b-cloud`) answer but
  never populate `logprobs` at all — only genuinely local models do.
  Detected on the real call, not `hasattr`, since it's a backend
  property invisible in advance.
- A thinking model's logprob-confidence check can return its reasoning
  trace instead of its answer — `qwen3.8` returned exactly one token,
  `"The"` (a sentence start), while the real answer never appeared in
  `logprobs` at all.

## Document ingestion & ontology quality

- `.bicep` and `.py` files route through plain-text loading — needed to
  check a document against the source code it actually documents,
  rather than another document about it.
- Extracted "endpoints" whose paths came from a schema *example* with
  no HTTP method attached — a URL string on its own isn't a callable
  operation.

## OpenSpec adapter

`tools/openspec-adapter/`

- WHEN-folding occasionally inverted a scenario's own sequencing
  bullets — an ordering-prefix ("only then," "after that") is now
  detected and preserved
  ([#5](https://github.com/QA-AU/claim-validator/issues/5)).
