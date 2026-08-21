"""The per-job Excel report — Claims, Gaps, Quality sheets — built from the
same `ValidationResult` the JSON API response comes from, so the two formats
can never drift apart from each other.
"""

from typing import Any, Dict

from phases.report_style import (
    VERDICT_FILL,
    VERDICT_HUMAN_CHECK,
    VERDICT_INCORRECT,
    VERDICT_QUALITY,
    style_sheet,
    write_row,
)

from claimvalidator.pipeline import ValidationResult


def claim_verdict(result_dict: Dict[str, Any]) -> str:
    """Quality / Human check / Incorrect, for one claim row.

    Mirrors `_requirement_verdict` in the source repo's run_report_excel.py,
    adapted to this system's verdicts (there is no shape-violation-implies-
    incorrect distinction to inherit here since a shape violation on a bare
    claim already means "nothing to judge" — folded into `no_evidence`-style
    handling rather than a separate branch).
    """
    if not result_dict["shape"]["ok"]:
        return VERDICT_INCORRECT
    verdict = result_dict["verdict"]
    if verdict == "contradicts":
        return VERDICT_INCORRECT
    if verdict == "no_evidence":
        return VERDICT_INCORRECT
    if verdict == "entails":
        return VERDICT_QUALITY
    return VERDICT_HUMAN_CHECK  # mentions_only, unjudged


def build_excel_report(result: ValidationResult, path: str) -> None:
    from pathlib import Path

    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)

    _claims_sheet(wb, result)
    _gaps_sheet(wb, result)
    _quality_sheet(wb, result)

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def _claims_sheet(wb, result: ValidationResult) -> None:
    from openpyxl.styles import Font, PatternFill

    sheet = wb.create_sheet("Claims")
    sheet.append(["Claim ID", "Text", "Shape", "Verdict", "Agreement",
                  "Cited chunks", "Reason"])

    r = 2
    for claim in result.per_claim:
        d = claim.to_dict()
        verdict = claim_verdict(d)
        write_row(sheet, r, [
            claim.id,
            claim.text,
            "OK" if claim.shape_ok else f"VIOLATION: {claim.shape_reason}",
            claim.verdict,
            claim.agreement or "-",
            ", ".join(str(c) for c in claim.cited_chunks) or "-",
            claim.reason,
        ], wrap_all_from=2)
        vcell = sheet.cell(row=r, column=4)
        vcell.font = Font(name="Arial", size=10, bold=True)
        vcell.fill = PatternFill("solid", fgColor=VERDICT_FILL[verdict])
        r += 1

    style_sheet(sheet, 1, [12, 50, 30, 16, 12, 16, 50])


def _gaps_sheet(wb, result: ValidationResult) -> None:
    sheet = wb.create_sheet("Gaps")
    sheet.append(["Concept", "Census range", "Probable instances",
                  "Addressed by claims", "Never addressed"])

    if not result.gap_report or not result.gap_report.ran:
        reason = result.gap_report.skipped_reason if result.gap_report else "not run"
        write_row(sheet, 2, ["(gap report not run)", "", "", "", reason], wrap_all_from=5)
        style_sheet(sheet, 1, [20, 14, 40, 18, 50])
        return

    r = 2
    for concept, gap in result.gap_report.per_concept.items():
        write_row(sheet, r, [
            concept,
            f"{gap.spread_low}–{gap.spread_high}",
            ", ".join(gap.probable) or "-",
            gap.addressed_count,
            ", ".join(gap.never_addressed) or "-",
        ], wrap_all_from=3)
        r += 1

    style_sheet(sheet, 1, [20, 14, 40, 18, 50])


def _quality_sheet(wb, result: ValidationResult) -> None:
    sheet = wb.create_sheet("Quality")
    sheet.append(["Metric", "Value"])

    r = 2
    for key, value in result.quality.items():
        write_row(sheet, r, [key, value])
        r += 1

    style_sheet(sheet, 1, [24, 14])
