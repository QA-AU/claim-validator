# Judge logic and data flow

A single, maintained diagram of what actually happens between a
submitted claim and its final verdict — kept current, not a snapshot.

**Update this when you change:** `claimvalidator/claim_retrieval.py`
(retrieval, compound-claim widening), `phases/requirement_shapes.py`
(shape check), `phases/entailment.py` (majority-vote judge,
consensus, escalation), `claimvalidator/logprob_judge.py` (logprob
judge), `claimvalidator/numeric_threshold_check.py` (structural
override), or the call order in `claimvalidator/pipeline.py::run_validation()`.
If a code change moves where a step happens relative to the others,
this diagram is wrong until it's edited to match.

![Judge logic and data flow](judge-flow.svg)

The image above (`judge-flow.svg`, same directory) is the diagram —
open it directly if your viewer doesn't render an `<img>` inside
markdown. It's a self-contained, hand-authored SVG (light/dark aware
via its own `prefers-color-scheme` media query), not a screenshot, so
editing it means editing the shapes and text in the file directly, the
same as any other source file. The Mermaid block below is kept as a
plain-text fallback and a quicker diff when only the logic changes,
not the layout — update both together.

```mermaid
flowchart TD
    DOC[Reference document] --> ONT["Ontology + RAG index<br/>cached by content hash"]
    CLAIM["Submitted claim: id + text"] --> RETRIEVE

    subgraph RETRIEVE["Retrieval — claim_retrieval.py"]
        direction TB
        R1{"Looks syntactically compound?<br/>comma/semicolon before a<br/>coordinating 'and'"}
        R1 -->|no| R2["Single retrieve_union query"]
        R1 -->|yes| R3["Whole-claim query<br/>+ one query per clause"]
        R3 --> R4["Union + dedupe chunks"]
    end
    ONT --> RETRIEVE
    RETRIEVE --> CHUNKS["Cited passages"]

    CHUNKS --> SHAPE["Shape check — deterministic,<br/>no LLM call<br/>requirement_shapes.py"]
    SHAPE -->|violation| FLAG["Flagged in the report,<br/>never dropped"]
    SHAPE --> JUDGE

    subgraph JUDGE["Entailment judge — entailment.py / logprob_judge.py"]
        direction TB
        J0{"judge_method"}
        J0 -->|"logprob — Ollama only"| JLP["Single pass;<br/>confidence read from<br/>token logprobs"]
        J0 -->|"majority_vote — default"| J1["3 runs × batches of 3 claims"]
        J1 --> J2["_consensus: majority verdict per claim"]
        J2 --> J3{"Doubtful?<br/>no majority, or a bare<br/>2-of-3 contradicts"}
        J3 -->|yes| J4["Escalate to a stronger model;<br/>re-judge the same cited passages"]
        J3 -->|no| J5["Verdict stands"]
        J4 --> J5
    end

    JLP --> VERDICT
    J5 --> VERDICT["entails / contradicts /<br/>mentions_only / no_evidence"]

    VERDICT --> NUM{"Verdict is entails<br/>or contradicts?"}
    NUM -->|no| FINAL
    NUM -->|yes| N1["Numeric threshold check —<br/>numeric_threshold_check.py"]
    N1 -->|"claim matches a recognized<br/>numeric shape, AND disagrees"| N2["Override verdict + reason;<br/>structurally_overridden = true"]
    N1 -->|"no match, or agrees"| FINAL
    N2 --> FINAL["Final per-claim verdict"]

    ONT -.-> CENSUS["Concept census —<br/>independent of any claim"]
    CENSUS --> GAP["Gap report — gap_report.py"]

    FINAL --> REPORT["Validation report<br/>JSON + Excel"]
    GAP --> REPORT
```

## Notes that don't fit in boxes

- **The shape check never gates the judge.** A claim that fails it is
  judged anyway — the violation is reported alongside the verdict, not
  instead of one. "Flagged, not dropped" (paper, §07).
- **The logprob path bypasses escalation entirely.** Escalation lives
  inside `judge_entailment()`'s own majority-vote flow; the logprob
  judge (`judge_entailment_logprob`, Ollama only) is a separate
  function the pipeline calls instead of it, not through it.
- **The numeric override runs after the judge, never before.** It only
  ever looks at a verdict the judge already reached, and only touches
  `entails`/`contradicts` — `mentions_only`/`no_evidence` are left
  alone on purpose (a regex match on its own shouldn't unilaterally
  reclassify those).
- **The census/gap-report branch is fully independent.** It runs off
  the ontology alone, never touches a claim's own verdict, and the two
  outputs are never merged into one score — see the paper's own
  mechanism diagram (§02) for why that separation is deliberate.
