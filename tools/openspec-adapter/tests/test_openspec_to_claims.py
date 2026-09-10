"""Tests for openspec_to_claims.py.

Run from the adapter directory:  python -m pytest tools/openspec-adapter -q
or:  cd tools/openspec-adapter && python -m pytest -q

These do not import claim-validator and need no third-party packages
beyond pytest itself.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import openspec_to_claims as o2c  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
CLI_LIST = FIX / "cli-list" / "spec.md"
SAMPLE_CHANGE = FIX / "sample-change" / "spec.md"


# ---- parsing -------------------------------------------------------------

def test_parses_every_requirement_and_scenario_of_a_full_spec():
    reqs = o2c.parse_spec(CLI_LIST)
    assert [r.name for r in reqs] == [
        "Command Execution", "Task Counting", "Output Format", "Flags",
        "Empty State", "Error Handling", "Sorting",
    ]
    assert all(r.delta_op == "none" for r in reqs)
    cmd_exec = reqs[0]
    assert cmd_exec.narrative.startswith("The command SHALL scan and analyze")
    assert [s.name for s in cmd_exec.scenarios] == [
        "Scanning for changes (default)", "Scanning for specs",
    ]
    assert cmd_exec.scenarios[0].whens == ["`openspec list` is executed without flags"]
    markers = [m for m, _ in cmd_exec.scenarios[0].assertions]
    assert markers == ["THEN", "AND", "AND"]


def test_delta_headers_tag_each_requirement_with_its_operation():
    reqs = o2c.parse_spec(SAMPLE_CHANGE)
    by_name = {r.name: r.delta_op for r in reqs}
    assert by_name == {
        "Token expiry": "added",
        "Token issuance": "modified",
        "Static API keys": "removed",
    }


def test_purpose_and_why_sections_are_not_mined_for_requirements():
    # cli-list has a trailing "## Why" section with prose bullet points.
    reqs = o2c.parse_spec(CLI_LIST)
    assert "Why" not in [r.name for r in reqs]
    joined = " ".join(r.narrative for r in reqs)
    assert "bird's-eye view" not in joined  # that phrase only lives under ## Why


def test_sub_bullets_under_a_then_are_folded_into_that_assertion():
    reqs = o2c.parse_spec(CLI_LIST)
    task_counting = next(r for r in reqs if r.name == "Task Counting")
    then_text = task_counting.scenarios[0].assertions[0][1]
    assert "`- [x]`" in then_text and "`- [ ]`" in then_text  # both sub-bullets captured


# ---- claim assembly ---------------------------------------------------------

def test_assertion_granularity_splits_each_then_and_bullet_into_its_own_claim():
    reqs = o2c.parse_spec(CLI_LIST)
    claims = o2c.build_claims(reqs, "cli-list", granularity="assertion")
    ids = [c.id for c in claims]
    # Command Execution's first scenario has THEN + AND + AND -> 3 claims
    assert "cli-list.R1.S1.A1" in ids
    assert "cli-list.R1.S1.A3" in ids
    assert "cli-list.R1.S1.A4" not in ids


def test_when_is_folded_in_as_a_condition_clause_verbatim():
    reqs = o2c.parse_spec(CLI_LIST)
    claims = o2c.build_claims(reqs, "cli-list", granularity="assertion")
    a2 = next(c for c in claims if c.id == "cli-list.R1.S1.A2")
    assert a2.text == (
        "When `openspec list` is executed without flags, exclude the "
        "`archive/` subdirectory from results."
    )


def test_exact_output_strings_from_the_spec_are_preserved_not_paraphrased():
    reqs = o2c.parse_spec(CLI_LIST)
    claims = o2c.build_claims(reqs, "cli-list", granularity="assertion")
    empty = next(c for c in claims if c.id == "cli-list.R5.S1.A1")
    assert '"No active changes found."' in empty.text
    exit_code = next(c for c in claims if c.id == "cli-list.R6.S2.A2")
    assert exit_code.text.endswith("exit with code 1.")


def test_requirement_narrative_becomes_its_own_shall_claim():
    reqs = o2c.parse_spec(CLI_LIST)
    claims = o2c.build_claims(reqs, "cli-list", granularity="assertion")
    r7 = next(c for c in claims if c.id == "cli-list.R7")
    assert r7.text == (
        "The command SHALL maintain consistent ordering of changes for "
        "predictable output."
    )
    assert r7.source_ref == "spec.md › Requirement: Sorting"


def test_removed_requirements_are_skipped_by_default_and_optional_on_request():
    reqs = o2c.parse_spec(SAMPLE_CHANGE)
    default = o2c.build_claims(reqs, "sample-change", include_removed=False)
    assert all("Static API keys" != c._requirement for c in default)
    with_removed = o2c.build_claims(reqs, "sample-change", include_removed=True)
    assert any(c._requirement == "Static API keys" for c in with_removed)


def test_delta_op_shows_up_in_source_ref():
    reqs = o2c.parse_spec(SAMPLE_CHANGE)
    claims = o2c.build_claims(reqs, "sample-change")
    added = next(c for c in claims if c.id == "sample-change.R1")
    assert "ADDED › Requirement: Token expiry" in added.source_ref
    modified = next(c for c in claims if c.id == "sample-change.R2")
    assert "MODIFIED › Requirement: Token issuance" in modified.source_ref


def test_scenario_granularity_makes_one_compound_claim_per_scenario():
    reqs = o2c.parse_spec(CLI_LIST)
    claims = o2c.build_claims(reqs, "cli-list", granularity="scenario")
    assert not any(".A" in c.id for c in claims)
    s1 = next(c for c in claims if c.id == "cli-list.R1.S1")
    assert "; and " in s1.text


def test_requirement_granularity_emits_only_shall_statements():
    reqs = o2c.parse_spec(CLI_LIST)
    claims = o2c.build_claims(reqs, "cli-list", granularity="requirement")
    assert len(claims) == 7
    assert all(c._marker == "SHALL" for c in claims)


def test_a_requirement_with_no_scenarios_still_yields_its_shall_claim(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(
        "# X\n\n## Requirements\n\n### Requirement: Bare\n"
        "The system SHALL do the thing.\n"
    )
    claims = o2c.build_claims(o2c.parse_spec(spec), "x")
    assert len(claims) == 1
    assert claims[0].id == "x.R1"


def test_a_scenario_with_no_then_is_skipped_with_a_warning(tmp_path, capsys):
    spec = tmp_path / "spec.md"
    spec.write_text(
        "# X\n\n## Requirements\n\n### Requirement: R\n"
        "The system SHALL do it.\n\n#### Scenario: broken\n"
        "- **WHEN** something happens\n"
    )
    claims = o2c.build_claims(o2c.parse_spec(spec), "x")
    assert [c.id for c in claims] == ["x.R1"]  # only the SHALL claim
    assert "no THEN/AND bullet" in capsys.readouterr().err


def test_multiple_whens_are_joined_with_and(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(
        "# X\n\n## Requirements\n\n### Requirement: R\nThe system SHALL x.\n\n"
        "#### Scenario: two conditions\n"
        "- **WHEN** the cache is cold\n- **WHEN** the user is signed in\n"
        "- **THEN** rebuild the cache\n"
    )
    claims = o2c.build_claims(o2c.parse_spec(spec), "x")
    a1 = next(c for c in claims if c.id == "x.R1.S1.A1")
    assert a1.text == "When the cache is cold and the user is signed in, rebuild the cache."


def test_claim_ids_are_unique_across_a_multi_requirement_spec():
    reqs = o2c.parse_spec(CLI_LIST)
    claims = o2c.build_claims(reqs, "cli-list")
    ids = [c.id for c in claims]
    assert len(ids) == len(set(ids))


def test_backticked_code_spans_survive_normalization():
    reqs = o2c.parse_spec(SAMPLE_CHANGE)
    claims = o2c.build_claims(reqs, "sample-change")
    grant = next(c for c in claims if c.id == "sample-change.R2.S1.A1")
    assert "`/oauth/token`" in grant.text and "`refresh_token`" in grant.text


# ---- output shape ---------------------------------------------------------

def test_json_output_is_exactly_the_three_claim_input_fields():
    reqs = o2c.parse_spec(CLI_LIST)
    claims = o2c.build_claims(reqs, "cli-list")
    parsed = json.loads(o2c._claims_json(claims))
    assert all(set(entry) == {"id", "text", "source_ref"} for entry in parsed)


def test_provenance_map_is_keyed_by_claim_id_and_carries_origin():
    reqs = o2c.parse_spec(SAMPLE_CHANGE)
    claims = o2c.build_claims(reqs, "sample-change")
    m = json.loads(o2c._provenance_map(claims))
    assert m["sample-change.R1.S1.A1"]["delta_op"] == "added"
    assert m["sample-change.R1.S1.A1"]["scenario"] == "A token past its lifetime is used"


# ---- golden fixtures ----------------------------------------------------------

@pytest.mark.parametrize("spec,golden,extra", [
    (CLI_LIST, FIX / "cli-list.claims.json", []),
    (SAMPLE_CHANGE, FIX / "sample-change.claims.json", ["--include-removed"]),
])
def test_cli_output_matches_committed_golden_file(spec, golden, extra):
    result = subprocess.run(
        [sys.executable, str(ROOT / "openspec_to_claims.py"), str(spec), *extra],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == json.loads(golden.read_text())
