#!/usr/bin/env python3
"""openspec_to_claims.py — normalize OpenSpec requirement specs into
claim-validator claim input.

Standalone: pure Python standard library, no third-party packages, no
network, and no import of claim-validator. It is a deterministic text
transform — parse the OpenSpec markdown grammar, split each requirement
into atomic assertions, phrase each as one declarative sentence, emit
JSON (or CSV) shaped as claim-validator's ClaimInput: {id, text,
source_ref}. No LLM is called anywhere in this file, by design: the
generated specs are the thing under test, so the step that prepares them
for judging must not itself be a model that could quietly fix or distort
them.

Usage:
    python openspec_to_claims.py PATH [PATH ...] [options]

PATH is an OpenSpec `spec.md` file, or a directory (searched recursively
for `spec.md`). Full specs (`## Requirements`) and delta specs
(`## ADDED / MODIFIED / REMOVED Requirements`) are both handled; the mode
is auto-detected per file from its section headers.

Options:
    --granularity {assertion,scenario,requirement}
                       assertion (default): one claim per THEN/AND bullet,
                         plus one for each requirement's SHALL statement.
                       scenario: one compound claim per scenario, plus the
                         SHALL statement.
                       requirement: only the SHALL statement per requirement.
    --capability NAME  Override the capability id used in claim ids and
                       provenance (default: the spec.md's parent directory
                       name, or the file stem).
    --include-removed  Also emit claims for `## REMOVED Requirements`
                       (skipped by default — a removal has nothing to
                       ground-check against a source document).
    --include-archived Also read spec.md files under an `archive/` directory
                       (skipped by default — OpenSpec keeps completed
                       changes there; they are history, not a proposal).
    --format {json,csv}   Output format (default: json).
    --map PATH         Also write a provenance map: {claim_id: {...}} with
                       the file, requirement, scenario, bullet, and delta
                       operation each claim came from.
    -o, --output PATH  Write output here (default: stdout).
    --stats            Print a one-line summary to stderr.

Exit status is non-zero only on an unreadable/empty input or a bad option.
Malformed individual requirements/scenarios are skipped with a warning on
stderr; they do not fail the run.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_SECTION_RE = re.compile(r"^##\s+(ADDED|MODIFIED|REMOVED)?\s*Requirements\s*$", re.IGNORECASE)
_OTHER_H2_RE = re.compile(r"^##\s+\S")
_REQ_RE = re.compile(r"^###\s+Requirement:\s*(.+?)\s*$", re.IGNORECASE)
_SCENARIO_RE = re.compile(r"^####\s+Scenario:\s*(.+?)\s*$", re.IGNORECASE)
# A step bullet: "- **WHEN** ...", "- WHEN ...", "* **THEN** ...", case-insensitive.
_STEP_RE = re.compile(r"^[-*]\s+(?:\*\*)?(WHEN|THEN|AND|GIVEN)(?:\*\*)?\s*:?\s*(.*)$", re.IGNORECASE)
# Any other bullet (a continuation / sub-bullet), captured with its indent.
_SUBBULLET_RE = re.compile(r"^(\s+)[-*]\s+(.*)$")
_PLAIN_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")


@dataclass
class Scenario:
    name: str
    whens: List[str] = field(default_factory=list)
    # (marker, text) where marker is THEN / AND / GIVEN-as-context excluded
    assertions: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class Requirement:
    name: str
    narrative: str = ""
    scenarios: List[Scenario] = field(default_factory=list)
    delta_op: str = "none"  # none | added | modified | removed
    source_file: str = ""


def _strip_markdown(text: str) -> str:
    """Collapse whitespace/newlines to single spaces and drop bold markers.
    Backticked code spans are preserved verbatim — they carry meaning."""
    text = text.replace("**", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_spec(path: Path) -> List[Requirement]:
    """Parse one OpenSpec spec.md into a list of Requirement objects."""
    lines = path.read_text(encoding="utf-8").splitlines()

    requirements: List[Requirement] = []
    in_requirements_section = False
    current_delta_op = "none"
    req: Optional[Requirement] = None
    scenario: Optional[Scenario] = None
    # Where free text is currently being collected: "narrative" | "step" | None
    collecting = None
    narrative_buf: List[str] = []
    last_step_idx: Optional[int] = None  # index into scenario.assertions, or -1 for a WHEN

    def flush_narrative():
        nonlocal narrative_buf
        if req is not None and narrative_buf:
            joined = _strip_markdown(" ".join(narrative_buf))
            req.narrative = (req.narrative + " " + joined).strip() if req.narrative else joined
        narrative_buf = []

    for raw in lines:
        line = raw.rstrip("\n")

        m_section = _SECTION_RE.match(line)
        if m_section:
            flush_narrative()
            in_requirements_section = True
            kind = (m_section.group(1) or "").lower()
            current_delta_op = kind if kind else "none"
            req = None
            scenario = None
            collecting = None
            continue

        # Any other H2 ends the requirements section.
        if _OTHER_H2_RE.match(line) and not m_section:
            flush_narrative()
            in_requirements_section = False
            req = None
            scenario = None
            collecting = None
            continue

        if not in_requirements_section:
            continue

        m_req = _REQ_RE.match(line)
        if m_req:
            flush_narrative()
            req = Requirement(name=m_req.group(1).strip(), delta_op=current_delta_op,
                              source_file=str(path))
            requirements.append(req)
            scenario = None
            collecting = "narrative"
            last_step_idx = None
            continue

        m_scn = _SCENARIO_RE.match(line)
        if m_scn:
            flush_narrative()
            if req is None:
                sys.stderr.write(
                    f"warning: {path}: scenario '{m_scn.group(1).strip()}' with no "
                    f"preceding requirement — skipped\n"
                )
                scenario = None
                collecting = None
                continue
            scenario = Scenario(name=m_scn.group(1).strip())
            req.scenarios.append(scenario)
            collecting = "step"
            last_step_idx = None
            continue

        m_step = _STEP_RE.match(line)
        if m_step and scenario is not None:
            marker = m_step.group(1).upper()
            body = _strip_markdown(m_step.group(2))
            if marker in ("WHEN", "GIVEN"):
                if body:
                    scenario.whens.append(body)
                last_step_idx = -1
            else:  # THEN / AND
                scenario.assertions.append((marker, body))
                last_step_idx = len(scenario.assertions) - 1
            collecting = "step"
            continue

        m_sub = _SUBBULLET_RE.match(line)
        if m_sub and scenario is not None and collecting == "step":
            extra = _strip_markdown(m_sub.group(2))
            if last_step_idx is None:
                pass
            elif last_step_idx == -1 and scenario.whens:
                scenario.whens[-1] = f"{scenario.whens[-1]} {extra}".strip()
            elif last_step_idx >= 0:
                marker, txt = scenario.assertions[last_step_idx]
                scenario.assertions[last_step_idx] = (marker, f"{txt} {extra}".strip())
            continue

        # A plain (non-step, non-indented) bullet inside a scenario: treat as
        # continuation of the last assertion so nothing is silently dropped.
        m_plain = _PLAIN_BULLET_RE.match(line)
        if m_plain and scenario is not None and collecting == "step" and last_step_idx is not None:
            extra = _strip_markdown(m_plain.group(1))
            if last_step_idx == -1 and scenario.whens:
                scenario.whens[-1] = f"{scenario.whens[-1]} {extra}".strip()
            elif last_step_idx >= 0:
                marker, txt = scenario.assertions[last_step_idx]
                scenario.assertions[last_step_idx] = (marker, f"{txt} {extra}".strip())
            continue

        if collecting == "narrative" and line.strip():
            narrative_buf.append(line.strip())
            continue
        # Blank line or stray content inside a scenario: ignore.

    flush_narrative()
    return requirements


# --------------------------------------------------------------------------
# Claim assembly
# --------------------------------------------------------------------------

_ACRONYM_RE = re.compile(r"^[A-Z]{2,}[A-Z0-9]*$")


def _lower_first(text: str) -> str:
    """Lowercase the first character unless the first token is an acronym,
    starts with a backtick, or is a path/flag — so `openspec list` and
    `GET /x` and `--flag` survive being spliced after 'When …, '."""
    if not text:
        return text
    first = text.split(" ", 1)[0].strip("`")
    if text[0] in "`-/" or _ACRONYM_RE.match(first):
        return text
    return text[0].lower() + text[1:]


def _as_sentence(text: str) -> str:
    text = text.strip().rstrip(".;,").strip()
    if not text:
        return text
    return (text[0].upper() + text[1:] + ".") if text[0].islower() else text + "."


def _condition(whens: List[str]) -> str:
    return " and ".join(w.strip().rstrip(".;,") for w in whens if w.strip())


def _assertion_claim_text(whens: List[str], assertion: str) -> str:
    cond = _condition(whens)
    if cond:
        return _as_sentence(f"When {_lower_first(cond)}, {_lower_first(assertion)}")
    return _as_sentence(assertion)


def _scenario_claim_text(whens: List[str], assertions: List[Tuple[str, str]]) -> str:
    body = "; and ".join(_lower_first(a.rstrip(".;,")) for _, a in assertions if a.strip())
    cond = _condition(whens)
    if cond:
        return _as_sentence(f"When {_lower_first(cond)}, {body}")
    return _as_sentence(body)


@dataclass
class Claim:
    id: str
    text: str
    source_ref: str
    # provenance, only written to the --map file
    _file: str = ""
    _capability: str = ""
    _requirement: str = ""
    _scenario: str = ""
    _marker: str = ""
    _delta_op: str = "none"


def _capability_for(path: Path, override: Optional[str]) -> str:
    if override:
        return override
    parent = path.parent.name
    if parent and parent not in (".", "specs", "changes"):
        return parent
    return path.stem


def build_claims(
    requirements: Iterable[Requirement],
    capability: str,
    granularity: str = "assertion",
    include_removed: bool = False,
) -> List[Claim]:
    claims: List[Claim] = []
    r_index = 0
    for req in requirements:
        if req.delta_op == "removed" and not include_removed:
            continue
        r_index += 1
        rid = f"{capability}.R{r_index}"
        op_tag = "" if req.delta_op == "none" else f"{req.delta_op.upper()} › "

        if req.narrative:
            claims.append(Claim(
                id=rid,
                text=_as_sentence(req.narrative),
                source_ref=f"{Path(req.source_file).name} › {op_tag}Requirement: {req.name}",
                _file=req.source_file, _capability=capability, _requirement=req.name,
                _marker="SHALL", _delta_op=req.delta_op,
            ))
        else:
            sys.stderr.write(f"warning: requirement '{req.name}' has no narrative text — "
                             f"no SHALL claim emitted\n")

        if granularity == "requirement":
            continue

        for s_index, scn in enumerate(req.scenarios, start=1):
            if not scn.assertions:
                sys.stderr.write(f"warning: scenario '{scn.name}' (requirement '{req.name}') "
                                 f"has no THEN/AND bullet — skipped\n")
                continue
            base_ref = (f"{Path(req.source_file).name} › {op_tag}Requirement: {req.name} "
                        f"› Scenario: {scn.name}")
            if granularity == "scenario":
                claims.append(Claim(
                    id=f"{rid}.S{s_index}",
                    text=_scenario_claim_text(scn.whens, scn.assertions),
                    source_ref=base_ref,
                    _file=req.source_file, _capability=capability, _requirement=req.name,
                    _scenario=scn.name, _marker="SCENARIO", _delta_op=req.delta_op,
                ))
            else:  # assertion
                for a_index, (marker, atext) in enumerate(scn.assertions, start=1):
                    if not atext.strip():
                        continue
                    claims.append(Claim(
                        id=f"{rid}.S{s_index}.A{a_index}",
                        text=_assertion_claim_text(scn.whens, atext),
                        source_ref=f"{base_ref} › {marker}[{a_index}]",
                        _file=req.source_file, _capability=capability, _requirement=req.name,
                        _scenario=scn.name, _marker=marker, _delta_op=req.delta_op,
                    ))
    return claims


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------

def _iter_spec_files(paths: List[str], include_archived: bool = False) -> List[Path]:
    out: List[Path] = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            found = sorted(pp.rglob("spec.md"))
            if not include_archived:
                # OpenSpec keeps completed changes under openspec/changes/archive/.
                # Those are history, not a proposal under review — skip them
                # unless asked. An explicitly named file is always honored.
                found = [f for f in found if "archive" not in f.parts]
            out.extend(found)
        elif pp.is_file():
            out.append(pp)
        else:
            raise SystemExit(f"error: no such file or directory: {p}")
    if not out:
        raise SystemExit("error: no spec.md files found in the given paths")
    return out


def _claims_json(claims: List[Claim]) -> str:
    return json.dumps(
        [{"id": c.id, "text": c.text, "source_ref": c.source_ref} for c in claims],
        indent=2, ensure_ascii=False,
    )


def _claims_csv(claims: List[Claim]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "text", "source_ref"])
    for c in claims:
        w.writerow([c.id, c.text, c.source_ref])
    return buf.getvalue()


def _provenance_map(claims: List[Claim]) -> str:
    return json.dumps(
        {
            c.id: {
                "file": c._file,
                "capability": c._capability,
                "requirement": c._requirement,
                "scenario": c._scenario,
                "marker": c._marker,
                "delta_op": c._delta_op,
                "text": c.text,
            }
            for c in claims
        },
        indent=2, ensure_ascii=False,
    )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="openspec_to_claims.py",
        description="Normalize OpenSpec requirement specs into claim-validator claim input.",
    )
    ap.add_argument("paths", nargs="+", help="spec.md file(s) or directory(ies)")
    ap.add_argument("--granularity", choices=["assertion", "scenario", "requirement"],
                    default="assertion")
    ap.add_argument("--capability", default=None,
                    help="override the capability id (default: spec.md parent dir name)")
    ap.add_argument("--include-removed", action="store_true",
                    help="also emit REMOVED requirements (skipped by default)")
    ap.add_argument("--include-archived", action="store_true",
                    help="also read spec.md files under an archive/ directory "
                         "(skipped by default — completed changes, not proposals)")
    ap.add_argument("--format", choices=["json", "csv"], default="json")
    ap.add_argument("--map", dest="map_path", default=None,
                    help="also write a provenance map JSON to this path")
    ap.add_argument("-o", "--output", default=None, help="output file (default: stdout)")
    ap.add_argument("--stats", action="store_true", help="print a summary to stderr")
    args = ap.parse_args(argv)

    files = _iter_spec_files(args.paths, include_archived=args.include_archived)

    all_claims: List[Claim] = []
    per_cap_offset: dict = {}
    for f in files:
        cap = _capability_for(f, args.capability)
        reqs = parse_spec(f)
        claims = build_claims(reqs, cap, args.granularity, args.include_removed)
        # If two input files resolve to the same capability, keep requirement
        # numbering monotonic across them so ids never collide.
        if cap in per_cap_offset:
            bump = per_cap_offset[cap]
            for c in claims:
                c.id = _renumber(c.id, cap, bump)
        per_cap_offset[cap] = per_cap_offset.get(cap, 0) + _max_r_index(claims, cap)
        all_claims.extend(claims)

    ids = [c.id for c in all_claims]
    if len(ids) != len(set(ids)):
        raise SystemExit("error: claim id collision — pass distinct --capability values "
                         "or process files one at a time")

    body = _claims_json(all_claims) if args.format == "json" else _claims_csv(all_claims)
    if args.output:
        Path(args.output).write_text(body + ("\n" if not body.endswith("\n") else ""),
                                     encoding="utf-8")
    else:
        sys.stdout.write(body + ("\n" if not body.endswith("\n") else ""))

    if args.map_path:
        Path(args.map_path).write_text(_provenance_map(all_claims) + "\n", encoding="utf-8")

    if args.stats:
        n_shall = sum(1 for c in all_claims if c._marker == "SHALL")
        n_assert = len(all_claims) - n_shall
        sys.stderr.write(
            f"{len(files)} spec file(s) → {len(all_claims)} claims "
            f"({n_shall} SHALL statements, {n_assert} scenario claims), "
            f"granularity={args.granularity}\n"
        )
    return 0


def _max_r_index(claims: List[Claim], cap: str) -> int:
    hi = 0
    for c in claims:
        m = re.match(rf"^{re.escape(cap)}\.R(\d+)", c.id)
        if m:
            hi = max(hi, int(m.group(1)))
    return hi


def _renumber(cid: str, cap: str, bump: int) -> str:
    m = re.match(rf"^{re.escape(cap)}\.R(\d+)(.*)$", cid)
    if not m:
        return cid
    return f"{cap}.R{int(m.group(1)) + bump}{m.group(2)}"


if __name__ == "__main__":
    raise SystemExit(main())
