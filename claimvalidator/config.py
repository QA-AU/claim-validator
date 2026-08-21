"""Environment-driven configuration: provider/model, storage paths, output dirs."""

import os

STORE_ROOT = os.getenv("CLAIMVAL_STORE_ROOT", "./.data/ontologies")
OUTPUT_DIR = os.getenv("CLAIMVAL_OUTPUT_DIR", "./.data/phase1_output")
REPORTS_DIR = os.getenv("CLAIMVAL_REPORTS_DIR", "./.data/reports")


def llm_client_factory():
    """A fresh client per call — safer than sharing one across concurrent
    background jobs whose usage tracking would otherwise mix."""
    from phases.cli_client import build_client

    model = os.getenv("CLAIMVAL_MODEL") or None
    provider = os.getenv("CLAIMVAL_PROVIDER") or None
    return build_client(model=model, provider=provider)
