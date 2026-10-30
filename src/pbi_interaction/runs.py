"""Expand the run tree in config.yaml into a flat, validated list of runs."""

import itertools
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .settings import ExportType, FilterSpec, RunConfig, RunNode, SlicerSpec

ScopeKey = tuple[str, str | None, str | None]  # (level, page, visual)


@dataclass(frozen=True)
class ResolvedSlicer:
    """A slicer setting with its friendly name resolved against the run's page."""

    ref: str  # as written in the config
    name: str | None
    id: str | None  # None: unresolved raw ref, looked up live
    page: str | None
    values: list[Any] | None
    raw: dict[str, Any] | None
    reset: bool
    default: list[Any] | None


@dataclass
class Run:
    label: str
    page: str
    visual: str
    export_type: ExportType
    filters: list[FilterSpec]
    slicers: list[ResolvedSlicer] = field(default_factory=list)

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


def safe(segment: str) -> str:
    return re.sub(r"[^\w.\- ]+", "_", str(segment)).strip() or "_"


def _merge_slicers(parent: list[SlicerSpec], child: list[SlicerSpec]) -> list[SlicerSpec]:
    """A child slicer setting replaces the parent's for the same slicer ref."""
    merged = {s.slicer: s for s in parent}
    merged.update({s.slicer: s for s in child})
    return list(merged.values())


def _matrix_children(axes: list[FilterSpec | SlicerSpec]) -> list[RunNode]:
    if any(isinstance(a, FilterSpec) and a.raw is not None for a in axes):
        raise ValueError("matrix axes must use table/column/values, not raw filters")
    per_axis = [[a.model_copy(update={"values": [v]}) for v in a.values] for a in axes]
    return [
        RunNode(
            label="_".join(safe(a.values[0]) for a in combo),
            filters=[a for a in combo if isinstance(a, FilterSpec)],
            slicers=[a for a in combo if isinstance(a, SlicerSpec)],
        )
        for combo in itertools.product(*per_axis)
    ]


def _resolve_slicer(cfg: RunConfig, spec: SlicerSpec, page: str) -> ResolvedSlicer:
    entries = cfg.slicer_names.get(spec.slicer)
    ref = None
    if entries:
        ref = next((e for e in entries if e.page == page), None) or next((e for e in entries if e.page is None), None)
        if ref is None:
            raise ValueError(f"slicer '{spec.slicer}' has no entry for page '{page}'")
    if ref and spec.values is not None and (bad := ref.invalid(spec.values)):
        raise ValueError(f"slicer '{spec.slicer}' on page '{page}': value {bad[0]!r} is not in its valid values")
    if spec.reset and (ref is None or ref.default is None):
        raise ValueError(f"slicer '{spec.slicer}': cannot reset without a default in slicer_names")
    return ResolvedSlicer(
        ref=spec.slicer,
        name=spec.slicer if ref else None,
        id=ref.id if ref else None,
        page=ref.page if ref else None,
        values=list(spec.values) if spec.values is not None else None,
        raw=spec.raw,
        reset=spec.reset,
        default=ref.default if ref else None,
    )


def expand(cfg: RunConfig) -> list[Run]:
    if not cfg.runs:
        roots = [RunNode(label=cfg.visual or "export")]
    else:
        roots = cfg.runs

    out: list[Run] = []

    def walk(node: RunNode, prefix: str, page, visual, etype, filters, slicers) -> None:
        label = f"{prefix}/{safe(node.label)}" if prefix else safe(node.label)
        page, visual = node.page or page, node.visual or visual
        etype = node.export_type or etype
        filters = _merge(filters, node.filters)
        slicers = _merge_slicers(slicers, node.slicers)
        kids = [*node.children, *(_matrix_children(node.matrix) if node.matrix else [])]
        if kids:
            for k in kids:
                walk(k, label, page, visual, etype, filters, slicers)
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
        slicers_resolved = [_resolve_slicer(cfg, s, page) for s in slicers]
        keys = [s.id or s.ref for s in slicers_resolved]
        if len(set(keys)) != len(keys):
            raise ValueError(f"run '{label}': sets the same slicer more than once")
        out.append(Run(label, page, visual, etype, resolved, slicers_resolved))

    for r in roots:
        walk(r, "", cfg.page, cfg.visual, cfg.export_type, list(cfg.filters), list(cfg.slicers))

    labels = [r.label for r in out]
    dupes = {label for label in labels if labels.count(label) > 1}
    if dupes:
        raise ValueError(f"duplicate run labels: {sorted(dupes)}")
    return out
