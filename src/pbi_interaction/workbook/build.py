"""Build each configured workbook from a batch's CSVs."""

from pathlib import Path
from typing import NamedTuple

from ..batch import load_manifest
from ..runs import safe
from ..settings import WorkbookSpec
from .merge import read_run, sheet_tables
from .plan import plan_workbook
from .write import Note, SheetEntry, write_workbook

USABLE = {"ok", "suspect"}


class BuildResult(NamedTuple):
    paths: list[Path]
    consumed: set[str]  # labels whose CSV went into a workbook


def build_workbooks(
    specs: list[WorkbookSpec], labels: list[str], batch_dir: Path,
    workbook: str | None = None,
) -> BuildResult:
    """Build each workbook from the batch's CSVs; failed or missing runs are skipped and noted."""
    manifest = load_manifest(batch_dir / "manifest.csv")
    usable = {
        lb: row["file"] for lb, row in manifest.items()
        if row["status"] in USABLE and row["file"] and (batch_dir / row["file"]).exists()
    }
    paths: list[Path] = []
    consumed: set[str] = set()
    for spec in specs:
        if workbook and spec.name != workbook:
            continue
        notes: list[Note] = [
            Note(lb, "omitted", manifest.get(lb, {}).get("status") or "not run") for lb in labels if lb not in usable
        ]
        notes += [Note(lb, "suspect", manifest[lb]["note"]) for lb in usable if manifest[lb]["status"] == "suspect"]
        sheets: list[SheetEntry] = []
        for plan in plan_workbook(spec, labels):
            frames = {lb: read_run(batch_dir, usable[lb]) for lb in plan.labels if lb in usable}
            if frames:
                consumed.update(frames)
                sheets += sheet_tables(plan, frames, notes)
        path = batch_dir / f"{safe(spec.name)}.xlsx"
        write_workbook(sheets, notes, path)
        paths.append(path)
    return BuildResult(paths, consumed)
