"""Resolve which runs land on which worksheet."""

import fnmatch
import logging
from dataclasses import dataclass, field

from ..settings import WorkbookSpec

log = logging.getLogger(__name__)


@dataclass
class SheetPlan:
    name: str
    labels: list[str]
    key: list[str] = field(default_factory=list)  # non-empty -> aggregate
    layout: str = "merged"


def plan_workbook(spec: WorkbookSpec, labels: list[str], *, partial: bool = False) -> list[SheetPlan]:
    """Resolve which runs land on which sheet. Raises ValueError on config/label mismatches.

    With `partial`, sheets matching none of `labels` are skipped with a warning instead of raising.
    """
    claimed: dict[str, str] = {}
    plans: list[SheetPlan] = []
    for sheet in spec.sheets:
        matched = [lb for lb in labels if any(fnmatch.fnmatchcase(lb, g) for g in sheet.runs)]
        if not matched and partial:
            log.warning("workbook '%s': sheet '%s' skipped, none of its runs are in scope", spec.name, sheet.name)
            continue
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
