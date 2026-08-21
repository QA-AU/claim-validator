"""`report_stem` only — extracted from the source repo's `phases/run_report.py`
(1000+ lines of Phase 1/2/3 plain-English run summarisation, none of which
applies here) so `phase1_storage.py` has something to import without pulling
in code shaped around a pipeline this repo doesn't have.
"""


def report_stem(phase: str, workflow_id: str) -> str:
    """The `<phase>_<id>` stem every per-run output file is named from.

    A command-line run defaults its workflow id to `phase1-<timestamp>`, so
    naming the file `<phase>_report_<workflow_id>` produced
    `phase1_report_phase1-1786973904.xlsx` — the phase said twice. Runs started
    from the web app carry short hex ids and never showed it.

    Shared rather than duplicated because two callers name this file — the
    end-of-run write and `cli_report export` — and if they ever disagreed the
    export would stop overwriting the run's own report and start accumulating a
    second copy of every run.
    """
    phase = (phase or "").strip()
    workflow_id = (workflow_id or "").strip()

    prefix = f"{phase}-"
    if phase and workflow_id.startswith(prefix):
        stripped = workflow_id[len(prefix):]
        # Only when something is left. An id that is exactly the phase name is
        # not a redundant prefix, it is the whole identifier.
        if stripped:
            return f"{phase}_{stripped}"

    if not workflow_id:
        # No id to distinguish it. `phase1__report.xlsx` reads as a bug.
        return phase or "run"
    return f"{phase}_{workflow_id}" if phase else workflow_id
