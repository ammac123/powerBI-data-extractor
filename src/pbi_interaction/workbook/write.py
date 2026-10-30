"""Write sheets to .xlsx with xlsxwriter."""

from numbers import Integral, Real
from pathlib import Path
from typing import Any, NamedTuple

import pandas as pd
import xlsxwriter
from xlsxwriter.format import Format
from xlsxwriter.worksheet import Worksheet

from ..errors import PbiError
from ..sheetnames import SHEET_NAME_MAX, sanitize_sheet_name

MAX_ROWS = 1_048_576
MAX_COLS = 16_384
MAX_EXACT_INT = 10**15  # Excel keeps ~15 significant digits; longer integers are written as text


class Table(NamedTuple):
    title: str | None  # title row above the table
    data: pd.DataFrame


class SheetEntry(NamedTuple):
    name: str
    tables: list[Table]


class Note(NamedTuple):
    run: str
    outcome: str
    detail: str


def unique_sheet_name(name: str, used: set[str]) -> str:
    base = sanitize_sheet_name(name)
    candidate, n = base, 1
    while candidate.lower() in used:
        n += 1
        suffix = f"~{n}"
        candidate = base[: SHEET_NAME_MAX - len(suffix)] + suffix
    used.add(candidate.lower())
    return candidate


def _write_cell(ws: Worksheet, row: int, col: int, value: Any) -> None:
    if pd.isna(value):
        return
    if isinstance(value, Integral) and abs(value) >= MAX_EXACT_INT:
        ws.write_string(row, col, str(value))
    elif isinstance(value, Real) and not isinstance(value, bool):
        ws.write_number(row, col, value)
    else:
        ws.write_string(row, col, str(value))


def _write_table(
    ws: Worksheet, df: pd.DataFrame, row: int, col: int, name: str, title: str | None, bold: Format,
) -> int:
    """Write one Excel Table at (row, col); returns the next free column."""
    if title:
        ws.write_string(row, col, title, bold)
        row += 1
    last_row = row + max(len(df), 1)
    if last_row >= MAX_ROWS or col + len(df.columns) > MAX_COLS:
        raise ValueError(f"table '{title or name}' ({len(df)} rows, {len(df.columns)} cols) exceeds Excel's limits")
    ws.add_table(row, col, last_row, col + len(df.columns) - 1, {
        "name": name, "columns": [{"header": str(c)} for c in df.columns],
    })
    for r, values in enumerate(df.astype(object).itertuples(index=False), row + 1):
        for c, v in enumerate(values, col):
            _write_cell(ws, r, c, v)
    for c, header in enumerate(df.columns, col):
        longest = max([len(str(header)), *df[header].dropna().astype(str).str.len().head(200)])
        ws.set_column(c, c, min(max(longest + 2, 10), 50))
    return col + len(df.columns) + 1


def _write_notes(wb: xlsxwriter.Workbook, notes: list[Note], used: set[str]) -> None:
    ws = wb.add_worksheet(unique_sheet_name("_notes", used))
    for c, h in enumerate(("run", "outcome", "detail")):
        ws.write_string(0, c, h, wb.add_format({"bold": True}))
    for r, note in enumerate(notes, 1):
        for c, v in enumerate(note):
            ws.write_string(r, c, str(v))
    ws.set_column(0, 2, 40)


def write_workbook(sheets: list[SheetEntry], notes: list[Note], path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    wb = xlsxwriter.Workbook(tmp, {"strings_to_numbers": False, "strings_to_formulas": False, "strings_to_urls": False})
    bold = wb.add_format({"bold": True})
    used: set[str] = set()
    for i, (name, tables) in enumerate(sheets, 1):
        ws = wb.add_worksheet(unique_sheet_name(name, used))
        col = 0
        for j, (title, df) in enumerate(tables, 1):
            col = _write_table(ws, df, 0, col, f"t{i}_{j}", title, bold)
    if notes:
        _write_notes(wb, notes, used)
    wb.close()
    try:
        tmp.replace(path)
    except OSError as e:
        raise PbiError(f"cannot write {path} (is it open in Excel?): {e}") from e
