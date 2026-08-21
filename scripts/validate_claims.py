"""Phase 1 vertical slice: one document, a handful of claims, no DB, no HTTP.

    python scripts/validate_claims.py DOC.txt claims.csv \
        --provider ollama --model qwen3.8:latest

Proves the shims in claimvalidator/claim_shims.py actually satisfy
judge_entailment's and check_requirement_shapes's duck-typing against a real
model, before anything (persistence, the gap report, the API) gets built on
top of it.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from phases.cli_client import build_client
from phases.entailment import judge_entailment
from phases.ontology_store import OntologyStore
from phases.phase1_models import Ontology
from phases.phase1_orchestrator import run_phase1
from phases.requirement_shapes import check_requirement_shapes

from claimvalidator.claim_retrieval import retrieve_for_claim
from claimvalidator.claim_shims import (
    ResolvedClaim,
    _ClaimSet,
    _JudgeClaim,
    shape_profile,
)
from claimvalidator.document_identity import resolve_ontology_key


def load_claims(path: str) -> list[ResolvedClaim]:
    claims = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            claims.append(ResolvedClaim(id=row["id"].strip(), text=row["text"].strip()))
    return claims


def main():
    parser = argparse.ArgumentParser(description="Validate claims against a document")
    parser.add_argument("document")
    parser.add_argument("claims_csv")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--store-root", default="./.data/ontologies")
    parser.add_argument("--shape-rules", default=None,
                         help="JSON dict overriding the default bare-claim shape rules")
    args = parser.parse_args()

    llm_client = build_client(model=args.model, provider=args.provider)
    claims = load_claims(args.claims_csv)
    print(f"Loaded {len(claims)} claim(s) from {args.claims_csv}", file=sys.stderr)

    store = OntologyStore(root=args.store_root)
    doc_name = Path(args.document).stem
    # Same content-hash cache resolve_validation (pipeline.py) uses — matching
    # the earlier version of this script bypassed it, so the same document run
    # twice under this script alone would never hit the cache. Fixed so both
    # entry points behave identically.
    key, reused = resolve_ontology_key(store, [args.document], document_id=doc_name)

    if not store.has_index(key):
        print(f"No cached ontology for '{doc_name}' — building one now...", file=sys.stderr)
        run_phase1(
            workflow_id=f"validate-{key}",
            name=doc_name,
            document_paths=[args.document],
            llm_client=llm_client,
            store=store,
            ontology_key=key,
            output_dir="./.data/phase1_output",
        )
    else:
        print(f"Reusing cached ontology for '{doc_name}' ({key})", file=sys.stderr)

    ontology = Ontology.from_dict(store.load_current(key))
    searcher = store.searcher_for(key)
    index = store.load_index(key)
    chunks: list[str] = index["chunks"]

    print(f"Retrieving supporting passages for {len(claims)} claim(s)...", file=sys.stderr)
    for claim in claims:
        result = retrieve_for_claim(claim.text, ontology, searcher, llm_client)
        claim.source_chunks = result.chunk_indices

    print("Running shape check...", file=sys.stderr)
    overrides = json.loads(args.shape_rules) if args.shape_rules else None
    shape_report = check_requirement_shapes(_ClaimSet(claims), profile=shape_profile(overrides))
    violations_by_id = {v.item_id: v.reason for v in shape_report.violations}

    print("Running entailment judge (3-run majority)...", file=sys.stderr)
    entailment_report = judge_entailment(
        [_JudgeClaim(c) for c in claims], chunks, llm_client,
    )
    verdicts_by_id = {v.requirement_id: v for v in entailment_report.verdicts}

    per_claim = []
    for claim in claims:
        verdict = verdicts_by_id.get(claim.id)
        per_claim.append({
            "id": claim.id,
            "text": claim.text,
            "shape": {
                "ok": claim.id not in violations_by_id,
                "reason": violations_by_id.get(claim.id),
            },
            "verdict": verdict.verdict if verdict else "unjudged",
            "judged": bool(verdict and verdict.judged),
            "agreement": f"{verdict.agreement}/{verdict.runs_judged}" if verdict else None,
            "cited_chunks": claim.source_chunks,
            "reason": verdict.reason if verdict else "no citation found by retrieval",
        })

    print(json.dumps({
        "document": args.document,
        "ontology_key": key,
        "shape_checked": shape_report.checked,
        "shape_violations": len(shape_report.violations),
        "entailed": len(entailment_report.entailed),
        "judged": len(entailment_report.judged),
        "per_claim": per_claim,
    }, indent=2))


if __name__ == "__main__":
    main()
