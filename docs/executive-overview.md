# Claim Validator — Executive Overview

*3 slides, plain text.*

---

## Slide 1 — The Problem

**LLMs are usually right. "Usually" is the risk.**

- Language models writing claims about a document — a contract summary, a compliance checklist, a technical report — get it wrong in a small but real share of cases.
- In a critical setting, that small error rate is the whole risk: one wrong claim, unnoticed, can cascade into a decision, a filing, or a downstream system.
- The only current safeguard is manual review of everything the model produced — which erases the time and cost savings of using the model in the first place.
- Existing AI-evaluation tools give one confidence score for a whole response. A low score tells you *something* is wrong — not which claim, or why. Not something a person can act on quickly.

**The gap: no independent, line-item check on what an LLM claims — without redoing the work by hand.**

---

## Slide 2 — How Claim Validator Solves It

**An automated second opinion, checked against the real source — not the model's own reasoning.**

- Every individual claim gets one of four plain outcomes: **confirmed**, **contradicted**, **not actually addressed**, or **never mentioned at all** — not a single blended score.
- Every verdict comes with the exact sentence from the source document it's based on — a reviewer (or another AI system) can verify it in seconds, not by re-reading the whole document.
- Separately flags **coverage gaps** — what the document contains that no claim ever touched — so a "100% correct" result can't hide a mostly-unreviewed document.
- Deployed as the organization's **own controlled infrastructure** — own database, own security boundary, own audit trail — not a shared third-party service handling sensitive documents.

**Result: catches the small share of wrong claims, without requiring the other 95%+ to be manually re-verified.**

---

## Slide 3 — Where It's Used, and the Bottom Line

**Any workflow where an LLM's claims about a document will be acted on:**
- Contract and license review
- Regulatory and compliance checklists
- Clinical and technical guidance summaries
- Incident and audit reports

**Two ways in, same result:**
- A person uploads a document and claims, reviews the verdicts, downloads a report.
- An AI agent calls the same interface directly — checking its own draft output before it's finalized, with no extra integration work required.

**The business case:**
- Keeps the speed and cost advantage of using an LLM.
- Removes the "trust but can't verify" gap that currently forces a choice between manual re-checking and unmanaged risk.
- Runs inside infrastructure the organization already controls — no new third-party data-sharing exposure.
