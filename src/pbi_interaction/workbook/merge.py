"""Join and shape run frames into sheet entries."""

import logging
from functools import reduce
from pathlib import Path

import pandas as pd

from ..clean import coerce_numbers
from .plan import SheetPlan
from .write import Note, SheetEntry, Table

log = logging.getLogger(__name__)


def _short_names(labels: list[str]) -> dict[str, str]:
    """Last label segment when unique among `labels`, else the whole label."""
    last = {lb: lb.rsplit("/", 1)[-1] for lb in labels}
    return {lb: seg if list(last.values()).count(seg) == 1 else lb.replace("/", "_") for lb, seg in last.items()}


def merge_frames(frames: dict[str, pd.DataFrame], key: list[str]) -> pd.DataFrame:
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


def _check_keys(frames: dict[str, pd.DataFrame], key: list[str]) -> None:
    for lb, df in frames.items():
        if missing := [k for k in key if k not in df.columns]:
            raise ValueError(f"run '{lb}' has no key column(s) {missing}; columns: {list(df.columns)}")


def read_run(batch_dir: Path, file: str) -> pd.DataFrame:
    df = pd.read_csv(batch_dir / file, dtype=str, keep_default_na=False).replace("", pd.NA)
    return coerce_numbers(df)


def sheet_tables(
    plan: SheetPlan, frames: dict[str, pd.DataFrame], notes: list[Note],
) -> list[SheetEntry]:
    """Turn a plan and its loaded frames into sheet entries."""
    if not plan.key or len(frames) == 1:
        return [SheetEntry(plan.name, [Table(None, next(iter(frames.values())))])]
    _check_keys(frames, plan.key)
    if plan.layout == "blocks":
        return [SheetEntry(plan.name, [Table(lb, df) for lb, df in frames.items()])]
    sheets: list[SheetEntry] = []
    joinable: dict[str, pd.DataFrame] = {}
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
