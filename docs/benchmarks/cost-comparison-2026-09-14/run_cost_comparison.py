"""Real cost comparison: Claim Validator (ontology build + judge) vs
DeepEval's FaithfulnessMetric, on a small, previously-unused document and
a 6-claim set, using actual Anthropic token usage from both systems --
no estimation, no reused/cached numbers.
"""
import json
import os
import sys
from pathlib import Path

SCRATCH = "/private/tmp/claude-501/-Users-qaziazam/9803a319-11a8-4534-9820-6af68d722029/scratchpad"
REPO = "/Users/qaziazam/claim-validator"
COST_DIR = f"{SCRATCH}/cost_test"

with open(f"{SCRATCH}/anthropic_key.txt") as f:
    os.environ["ANTHROPIC_API_KEY"] = f.read().strip()

sys.path.insert(0, REPO)
sys.path.insert(0, SCRATCH)

from phases.cli_client import build_client
from phases.entailment import judge_entailment
from phases.ontology_store import OntologyStore
from phases.phase1_models import Ontology
from phases.phase1_orchestrator import run_phase1
from phases.llm_usage import usage_of, TokenRates

from claimvalidator.claim_retrieval import retrieve_for_claim
from claimvalidator.claim_shims import ResolvedClaim, _JudgeClaim
from claimvalidator.document_identity import resolve_ontology_key

# Haiku 4.5 published pricing (2026-09-14, anthropic.com/pricing):
# $1 / MTok input, $5 / MTok output.
RATES = TokenRates(input_per_mtok_cents=100, output_per_mtok_cents=500)

DOC_PATH = f"{COST_DIR}/office-supplies-policy.md"
CLAIMS = json.load(open(f"{COST_DIR}/claims.json"))

llm_client = build_client(model="claude-haiku-4-5", provider="anthropic")
print(f"Model: {llm_client.model}", file=sys.stderr)

store = OntologyStore(root=f"{COST_DIR}/.data/ontologies")
doc_name = Path(DOC_PATH).stem
key, reused = resolve_ontology_key(store, [DOC_PATH], document_id=doc_name)
print(f"Ontology key: {key}  reused={reused}", file=sys.stderr)

def snap(client):
    u = usage_of(client)
    return (u.calls, u.input_tokens, u.output_tokens)

usage_start = snap(llm_client)
print(f"Usage before ontology build: {usage_start}", file=sys.stderr)

if not store.has_index(key):
    print("Building ontology (real, uncached document)...", file=sys.stderr)
    run_phase1(
        workflow_id=f"cost-test-{key}",
        name=doc_name,
        document_paths=[DOC_PATH],
        llm_client=llm_client,
        store=store,
        ontology_key=key,
        background_description="An internal company office-supplies reimbursement policy.",
        output_dir=f"{COST_DIR}/.data/phase1_output",
    )
else:
    print("Ontology already cached from a prior run of this script -- rerun with a fresh store dir for a clean ontology-build measurement.", file=sys.stderr)

usage_after_ontology = snap(llm_client)
ontology_calls = usage_after_ontology[0] - usage_start[0]
ontology_in = usage_after_ontology[1] - usage_start[1]
ontology_out = usage_after_ontology[2] - usage_start[2]
print(f"Ontology build usage: calls={ontology_calls} in={ontology_in} out={ontology_out}", file=sys.stderr)

ontology = Ontology.from_dict(store.load_current(key))
searcher = store.searcher_for(key)
index = store.load_index(key)
chunks = index["chunks"]

claims = [ResolvedClaim(id=c["id"], text=c["text"]) for c in CLAIMS]
print(f"Retrieving passages for {len(claims)} claims...", file=sys.stderr)
for claim in claims:
    result = retrieve_for_claim(claim.text, ontology, searcher, llm_client)
    claim.source_chunks = result.chunk_indices

usage_after_retrieval = snap(llm_client)  # retrieval is local TF-IDF, no LLM call expected
retrieval_calls = usage_after_retrieval[0] - usage_after_ontology[0]
print(f"Retrieval usage (should be ~0 LLM calls): calls={retrieval_calls}", file=sys.stderr)

print("Judging (3-run majority)...", file=sys.stderr)
entailment_report = judge_entailment([_JudgeClaim(c) for c in claims], chunks, llm_client)
verdicts_by_id = {v.requirement_id: v for v in entailment_report.verdicts}

usage_after_judge = snap(llm_client)
judge_calls = usage_after_judge[0] - usage_after_retrieval[0]
judge_in = usage_after_judge[1] - usage_after_retrieval[1]
judge_out = usage_after_judge[2] - usage_after_retrieval[2]
print(f"Judge usage (6 claims, 3-run majority): calls={judge_calls} in={judge_in} out={judge_out}", file=sys.stderr)

# Cite passages per claim, for feeding DeepEval identical evidence.
cited_passages_by_id = {}
for claim in claims:
    cited_passages_by_id[claim.id] = [chunks[i] for i in claim.source_chunks] or ["(no passage retrieved)"]

cv_results = {}
for claim in claims:
    v = verdicts_by_id.get(claim.id)
    cv_results[claim.id] = {
        "text": claim.text,
        "verdict": v.verdict if v else "unjudged",
        "cited_passages": cited_passages_by_id[claim.id],
    }

# ---- DeepEval, same 6 claims, same retrieved passages ----
print("\nRunning DeepEval FaithfulnessMetric on the same 6 claims...", file=sys.stderr)
from anthropic_deepeval_model import AnthropicDeepEvalModel
from deepeval.metrics import FaithfulnessMetric
from deepeval.test_case import LLMTestCase

de_model = AnthropicDeepEvalModel(model="claude-haiku-4-5")
metric = FaithfulnessMetric(threshold=0.5, model=de_model, include_reason=True, async_mode=False)

answer_key = json.load(open(f"{COST_DIR}/answer-key.json"))
de_rows = []
for claim in claims:
    cv = cv_results[claim.id]
    test_case = LLMTestCase(
        input="Is this claim faithfully supported by the source document?",
        actual_output=cv["text"],
        retrieval_context=cv["cited_passages"],
    )
    score = metric.measure(test_case)
    de_rows.append({
        "id": claim.id,
        "expected": answer_key[claim.id],
        "cv_verdict": cv["verdict"],
        "deepeval_score": score,
        "deepeval_faithful": metric.is_successful(),
        "deepeval_reason": metric.reason,
    })
    print(f"  {claim.id}: cv={cv['verdict']:14s} expected={answer_key[claim.id]:14s} "
          f"deepeval_score={score} deepeval_faithful={metric.is_successful()}", file=sys.stderr)

de_usage = de_model.usage
print(f"\nDeepEval usage (6 claims): calls={de_usage.calls} in={de_usage.input_tokens} out={de_usage.output_tokens}", file=sys.stderr)

# ---- Cost summary ----
def cost_dollars(in_tok, out_tok):
    cents = RATES.cost_cents(in_tok, out_tok)
    return cents / 100

summary = {
    "model": "claude-haiku-4-5",
    "rates": {"input_per_mtok_usd": 1.0, "output_per_mtok_usd": 5.0},
    "num_claims": len(claims),
    "claim_validator": {
        "ontology_build": {
            "calls": ontology_calls, "input_tokens": ontology_in, "output_tokens": ontology_out,
            "cost_usd": round(cost_dollars(ontology_in, ontology_out), 5),
        },
        "judge_6_claims": {
            "calls": judge_calls, "input_tokens": judge_in, "output_tokens": judge_out,
            "cost_usd": round(cost_dollars(judge_in, judge_out), 5),
        },
        "total_first_run": {
            "calls": ontology_calls + judge_calls,
            "input_tokens": ontology_in + judge_in,
            "output_tokens": ontology_out + judge_out,
            "cost_usd": round(cost_dollars(ontology_in + judge_in, ontology_out + judge_out), 5),
        },
        "marginal_per_claim_batch_after_ontology_cached": {
            "calls": judge_calls, "input_tokens": judge_in, "output_tokens": judge_out,
            "cost_usd": round(cost_dollars(judge_in, judge_out), 5),
        },
    },
    "deepeval": {
        "calls": de_usage.calls, "input_tokens": de_usage.input_tokens, "output_tokens": de_usage.output_tokens,
        "cost_usd": round(cost_dollars(de_usage.input_tokens, de_usage.output_tokens), 5),
    },
    "cv_verdicts": cv_results,
    "deepeval_rows": de_rows,
}

with open(f"{COST_DIR}/cost_comparison_result.json", "w") as f:
    json.dump(summary, f, indent=2)

print("\n\n=== SUMMARY ===")
print(json.dumps(summary["claim_validator"], indent=2))
print(json.dumps({"deepeval": summary["deepeval"]}, indent=2))
print("\nSaved to cost_comparison_result.json")
