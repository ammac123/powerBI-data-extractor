"""Expand the run tree in config.yaml into a flat, validated list of runs."""

import itertools
import json
import re
from dataclasses import dataclass
from typing import Any

from .settings import ExportType, FilterSpec, RunConfig, RunNode

ScopeKey = tuple[str, str | None, str | None]  # (level, page, visual)


@dataclass
class Run:
    label: str
    page: str
    visual: str
    export_type: ExportType
    filters: list[FilterSpec]

    def scopes(self) -> dict[ScopeKey, list[dict[str, Any]]]:
        """Group this run's SDK filters by the scope they are set on (report / page / visual)."""
        out: dict[ScopeKey, list[dict[str, Any]]] = {}
        for f in self.filters:
            key = (f.level, f.page if f.level != "report" else None, f.visual if f.level == "visual" else None)
            out.setdefault(key, []).append(f.sdk_filter())
        return out


def _filter_key(f: FilterSpec) -> tuple:
    target = json.dumps((f.raw or {}).get("target"), sort_keys=True) if f.raw is not None else (f.table, f.column)
    return (f.level, f.page, f.visual, target)


def _merge(parent: list[FilterSpec], child: list[FilterSpec]) -> list[FilterSpec]:
    """Child filters add to the parent's; a child filter on the same target replaces the parent's."""
    merged = {_filter_key(f): f for f in parent}
    merged.update({_filter_key(f): f for f in child})
    return list(merged.values())


def _safe(segment: str) -> str:
    return re.sub(r"[^\w.\- ]+", "_", str(segment)).strip() or "_"


def _matrix_children(axes: list[FilterSpec]) -> list[RunNode]:
    if any(f.raw is not None for f in axes):
        raise ValueError("matrix axes must use table/column/values, not raw filters")
    per_axis = [[f.model_copy(update={"values": [v]}) for v in f.values] for f in axes]
    return [
        RunNode(label="_".join(_safe(f.values[0]) for f in combo), filters=list(combo))
        for combo in itertools.product(*per_axis)
    ]


def expand(cfg: RunConfig) -> list[Run]:
    if not cfg.runs:
        roots = [RunNode(label=cfg.visual or "export")]
    else:
        roots = cfg.runs

    out: list[Run] = []

    def walk(node: RunNode, prefix: str, page, visual, etype, filters) -> None:
        label = f"{prefix}/{_safe(node.label)}" if prefix else _safe(node.label)
        page, visual = node.page or page, node.visual or visual
        etype = node.export_type or etype
        filters = _merge(filters, node.filters)
        kids = [*node.children, *(_matrix_children(node.matrix) if node.matrix else [])]
        if kids:
            for k in kids:
                walk(k, label, page, visual, etype, filters)
            return
        if not page or not visual:
            raise ValueError(f"run '{label}' has no page/visual (set them on the run, a parent, or at top level)")
        # Page/visual-level filters default to the run's own page/visual.
        resolved = [
            f.model_copy(update={
                "page": f.page or (page if f.level in ("page", "visual") else None),
                "visual": f.visual or (visual if f.level == "visual" else None),
            })
            for f in filters
        ]
        out.append(Run(label, page, visual, etype, resolved))

    for r in roots:
        walk(r, "", cfg.page, cfg.visual, cfg.export_type, list(cfg.filters))

    labels = [r.label for r in out]
    dupes = {label for label in labels if labels.count(label) > 1}
    if dupes:
        raise ValueError(f"duplicate run labels: {sorted(dupes)}")
    return out
