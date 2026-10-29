"""Build Excel workbooks from a batch's CSVs, as laid out by the `workbooks` config."""

import fnmatch
import logging
from dataclasses import dataclass, field
from functools import reduce
from numbers import Integral, Real
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Set

import pandas as pd
import xlsxwriter
from xlsxwriter.format import Format
from xlsxwriter.worksheet import Worksheet

from .batch import load_manifest
from .clean import coerce_numbers
from .errors import PbiError
from .runs import safe
from .settings import BAD_SHEET_NAME, SHEET_NAME_MAX, WorkbookSpec

log = logging.getLogger(__name__)

MAX_ROWS = 1_048_576
MAX_COLS = 16_384
MAX_EXACT_INT = 10**15  # Excel keeps ~15 significant digits; longer integers are written as text
USABLE = {"ok", "suspect"}



class Table(NamedTuple):
    title: Optional[str]  # title row above the table
    data: pd.DataFrame


class SheetEntry(NamedTuple):
    name: str
    tables: List[Table]


class Note(NamedTuple):
    run: str
    outcome: str
    detail: str


class BuildResult(NamedTuple):
    paths: List[Path]
    consumed: Set[str]  # labels whose CSV went into a workbook


@dataclass
class SheetPlan:
    name: str
    labels: List[str]
    key: List[str] = field(default_factory=list)  # non-empty -> aggregate
    layout: str = "merged"


def plan_workbook(spec: WorkbookSpec, labels: List[str]) -> List[SheetPlan]:
    """Resolve which runs land on which sheet. Pure; raises ValueError on config/label mismatches."""
    claimed: Dict[str, str] = {}
    plans: List[SheetPlan] = []
    for sheet in spec.sheets:
        matched = [lb for lb in labels if any(fnmatch.fnmatchcase(lb, g) for g in sheet.runs)]
        if not matched:
            raise ValueError(f"workbook '{spec.name}', sheet '{sheet.name}': no runs match {sheet.runs}")
        for lb in matched:
            if lb in claimed:
                raise ValueError(f"workbook '{spec.name}': run '{lb}' is on both '{claimed[lb]}' and '{sheet.name}'")
            claimed[lb] = sheet.name
        if sheet.mode == "aggregate":
            plans.append(SheetPlan(sheet.name, matched, sheet.key, sheet.layout))
        elif len(matched) == 1:
            plans.append(SheetPlan(sheet.name, matched))
        else:
            plans.extend(SheetPlan(lb, [lb]) for lb in matched)

    unassigned = [lb for lb in labels if lb not in claimed]
    if unassigned and spec.unassigned == "error":
        raise ValueError(f"workbook '{spec.name}': runs not on any sheet: {unassigned}")
    if unassigned and spec.unassigned == "sheet":
        log.warning("workbook '%s': runs not assigned to a sheet get their own: %s", spec.name, unassigned)
        plans.extend(SheetPlan(lb, [lb]) for lb in unassigned)
    return plans


def _short_names(labels: List[str]) -> Dict[str, str]:
    """Last label segment when unique among `labels`, else the whole label."""
    last = {lb: lb.rsplit("/", 1)[-1] for lb in labels}
    return {lb: seg if list(last.values()).count(seg) == 1 else lb.replace("/", "_") for lb, seg in last.items()}


def merge_frames(frames: Dict[str, pd.DataFrame], key: List[str]) -> pd.DataFrame:
    """Outer-join on `key`; every other column gets an `_<run>` suffix so headers stay unique."""
    if len(frames) == 1:
        return next(iter(frames.values()))
    names = _short_names(list(frames))
    parts = [
        df.rename(columns={c: f"{c}_{names[lb]}" for c in df.columns if c not in key}) for lb, df in frames.items()
    ]
    for k in key:  # a key typed differently across runs (e.g. int vs text) is joined as text
        if len({str(p[k].dtype) for p in parts}) > 1:
            parts = [p.astype({k: str}) for p in parts]
    return reduce(lambda a, b: a.merge(b, on=key, how="outer"), parts)


def _check_keys(frames: Dict[str, pd.DataFrame], key: List[str]) -> None:
    for lb, df in frames.items():
        if missing := [k for k in key if k not in df.columns]:
            raise ValueError(f"run '{lb}' has no key column(s) {missing}; columns: {list(df.columns)}")


def _read_run(batch_dir: Path, file: str) -> pd.DataFrame:
    df = pd.read_csv(batch_dir / file, dtype=str, keep_default_na=False).replace("", pd.NA)
    return coerce_numbers(df)


def _sheet_tables(
    plan: SheetPlan, frames: Dict[str, pd.DataFrame], notes: List[Note],
) -> List[SheetEntry]:
    """Turn a plan and its loaded frames into sheet entries."""
    if not plan.key or len(frames) == 1:
        return [SheetEntry(plan.name, [Table(None, next(iter(frames.values())))])]
    _check_keys(frames, plan.key)
    if plan.layout == "blocks":
        return [SheetEntry(plan.name, [Table(lb, df) for lb, df in frames.items()])]
    sheets: List[SheetEntry] = []
    joinable: Dict[str, pd.DataFrame] = {}
    for lb, df in frames.items():
        if df.duplicated(plan.key).any():
            log.warning("%s: duplicate values in key %s; written to its own sheet", lb, plan.key)
            notes.append(Note(lb, "own sheet", f"duplicate values in key {plan.key}"))
            sheets.append(SheetEntry(lb, [Table(None, df)]))
        else:
            joinable[lb] = df
    if joinable:
        sheets.insert(0, SheetEntry(plan.name, [Table(None, merge_frames(joinable, plan.key))]))
    return sheets


def _sheet_name(name: str, used: Set[str]) -> str:
    base = BAD_SHEET_NAME.sub("_", name).strip("'")[:SHEET_NAME_MAX] or "_"
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
    ws: Worksheet, df: pd.DataFrame, row: int, col: int, name: str, title: Optional[str], bold: Format,
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


def _write_notes(wb: xlsxwriter.Workbook, notes: List[Note], used: Set[str]) -> None:
    ws = wb.add_worksheet(_sheet_name("_notes", used))
    for c, h in enumerate(("run", "outcome", "detail")):
        ws.write_string(0, c, h, wb.add_format({"bold": True}))
    for r, note in enumerate(notes, 1):
        for c, v in enumerate(note):
            ws.write_string(r, c, str(v))
    ws.set_column(0, 2, 40)


def write_workbook(sheets: List[SheetEntry], notes: List[Note], path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    wb = xlsxwriter.Workbook(tmp, {"strings_to_numbers": False, "strings_to_formulas": False, "strings_to_urls": False})
    bold = wb.add_format({"bold": True})
    used: Set[str] = set()
    for i, (name, tables) in enumerate(sheets, 1):
        ws = wb.add_worksheet(_sheet_name(name, used))
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


def build_workbooks(
    specs: List[WorkbookSpec], labels: List[str], batch_dir: Path, only: Optional[str] = None,
) -> BuildResult:
    """Build each workbook from the batch's CSVs; failed or missing runs are skipped and noted."""
    manifest = load_manifest(batch_dir / "manifest.csv")
    usable = {
        lb: row["file"] for lb, row in manifest.items()
        if lb in labels and row["status"] in USABLE and row["file"] and (batch_dir / row["file"]).exists()
    }
    paths: List[Path] = []
    consumed: Set[str] = set()
    for spec in specs:
        if only and spec.name != only:
            continue
        notes: List[Note] = [
            Note(lb, "omitted", manifest.get(lb, {}).get("status") or "not run") for lb in labels if lb not in usable
        ]
        notes += [Note(lb, "suspect", manifest[lb]["note"]) for lb in usable if manifest[lb]["status"] == "suspect"]
        sheets: List[SheetEntry] = []
        for plan in plan_workbook(spec, labels):
            frames = {lb: _read_run(batch_dir, usable[lb]) for lb in plan.labels if lb in usable}
            if frames:
                consumed.update(frames)
                sheets += _sheet_tables(plan, frames, notes)
        path = batch_dir / f"{safe(spec.name)}.xlsx"
        write_workbook(sheets, notes, path)
        paths.append(path)
    return BuildResult(paths, consumed)
