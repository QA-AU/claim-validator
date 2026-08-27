"""phases/report_style.py's write_row — specifically its formula-injection
guard. claim.text and claim.id are caller-supplied and land in report cells
unchanged; found unguarded in a vulnerability scan (CWE-1236: a cell
starting with =, +, -, or @ is evaluated as a formula by Excel/LibreOffice
once the file is opened).
"""

from openpyxl import Workbook

from phases.report_style import write_row


def _written(value):
    wb = Workbook()
    sheet = wb.active
    write_row(sheet, 1, [value])
    return sheet.cell(row=1, column=1).value


def test_a_leading_equals_sign_is_defused():
    assert _written("=cmd|'/c calc'!A0") == "'=cmd|'/c calc'!A0"


def test_a_leading_plus_minus_or_at_sign_is_defused():
    assert _written("+1+1").startswith("'")
    assert _written("-1+1").startswith("'")
    assert _written("@SUM(A1)").startswith("'")


def test_ordinary_claim_text_is_left_alone():
    assert _written("the invoice total is $500") == "the invoice total is $500"


def test_non_string_values_pass_through_unchanged():
    assert _written(42) == 42
    assert _written(None) is None
