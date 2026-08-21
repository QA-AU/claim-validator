"""Environment-driven configuration: provider/model, storage paths, output dirs."""

import os

STORE_ROOT = os.getenv("CLAIMVAL_STORE_ROOT", "./.data/ontologies")
OUTPUT_DIR = os.getenv("CLAIMVAL_OUTPUT_DIR", "./.data/phase1_output")
# Not under .data/, unlike the two above: those are internal machinery
# (the DB, the ontology cache) nobody needs to browse by hand. A report is
# the one generated artefact meant for a person to open, so it lives
# somewhere Finder/Explorer shows without toggling hidden files — still
# gitignored, just not hidden from the person it's for.
REPORTS_DIR = os.getenv("CLAIMVAL_REPORTS_DIR", "./reports")


def llm_client_factory():
    """A fresh client per call — safer than sharing one across concurrent
    background jobs whose usage tracking would otherwise mix."""
    from phases.cli_client import build_client

    model = os.getenv("CLAIMVAL_MODEL") or None
    provider = os.getenv("CLAIMVAL_PROVIDER") or None
    return build_client(model=model, provider=provider)
