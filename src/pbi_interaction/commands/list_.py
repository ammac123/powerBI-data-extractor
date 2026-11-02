"""`list`: list pages and visuals, or slicers (optionally as a `visual_names` or `slicer_names` stub)."""

import argparse
import json
import re
from collections.abc import Callable

from ..context import Context
from ..settings import BASIC_FILTER_TYPE
from .base import Command


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--slicers", action="store_true", help="list slicers with target and current selection")
    parser.add_argument("--page", help="with --slicers/--stub/--visual-stub: only this page")
    stub = parser.add_mutually_exclusive_group()
    stub.add_argument("--stub", action="store_true", help="print a `slicer_names:` YAML block")
    stub.add_argument("--visual-stub", action="store_true", help="print a `visual_names:` YAML block")


def _slug(text: str) -> str:
    return re.sub(r"\W+", "_", text.lower()).strip("_")


def _target(row: dict) -> str:
    return ", ".join(f"{t.get('table')}.{t.get('column')}" for t in row.get("targets") or [])


def _unique_names(rows: list[dict], fallback: Callable[[dict], str]) -> list[str]:
    taken: set[str] = set()
    names = []
    for row in rows:
        base = _slug(row.get("title") or "") or fallback(row)
        name = base
        if name in taken:
            name = f"{base}_{_slug(row['page'])}"
        suffix = 2
        while name in taken:
            name = f"{base}_{_slug(row['page'])}_{suffix}"
            suffix += 1
        taken.add(name)
        names.append(name)
    return names


def _slicer_fallback_name(row: dict) -> str:
    column = ((row.get("targets") or [{}])[0].get("column")) or ""
    return f"{_slug(column)}_slicer"


def slicer_stub(rows: list[dict]) -> str:
    """Render a ready-to-paste `slicer_names:` block; `values:` (valid values) is left to the user."""
    lines = ["slicer_names:"]
    for name, row in zip(_unique_names(rows, _slicer_fallback_name), rows, strict=True):
        if row.get("stateError"):
            lines.append(f"  # {name}: {row['name']} on {row['page']!r} skipped: {row['stateError']}")
            continue
        unselected = row["filterType"] is None
        filter_type = BASIC_FILTER_TYPE if unselected else row["filterType"]
        entry = {"id": row["name"], "page": row["page"], "filter_type": filter_type}
        basic = filter_type == BASIC_FILTER_TYPE
        if basic:
            entry["default"] = list(row.get("values") or [])
        body = ", ".join(f"{k}: {json.dumps(v)}" for k, v in entry.items())
        note = f"{row.get('title')!r} {row['type']} -> {_target(row)}"
        if unselected:
            note += "; nothing selected: assumed Basic"
        elif not basic:
            note += f"; filter_type {row['filterType']}: no default, set via raw"
        lines.append(f"  {name}: {{{body}}}  # {note}")
    return "\n".join(lines)


def visual_stub(rows: list[dict]) -> str:
    """Render a ready-to-paste `visual_names:` block; slicers belong in `slicer_names`."""
    rows = [r for r in rows if "slicer" not in r["type"].lower()]
    lines = ["visual_names:"]
    for name, row in zip(_unique_names(rows, lambda r: _slug(f"{r['type']} visual")), rows, strict=True):
        body = f"id: {json.dumps(row['name'])}, page: {json.dumps(row['page'])}"
        lines.append(f"  {name}: {{{body}}}  # {row['title']!r} {row['type']}")
    return "\n".join(lines)


def _print_slicers(rows: list[dict]) -> None:
    for r in rows:
        error = f" | state unreadable: {r['stateError']}" if r.get("stateError") else ""
        print(
            f"{r['page']!r:20} | {r['title']!r:25} | {r['type']:12} | {r['filterType']} | "
            f"{_target(r):30} | {r['values']} | {r['name']}{error}"
        )


def run(ctx: Context, args: argparse.Namespace) -> None:
    with ctx.session() as s:
        if args.visual_stub:
            print(visual_stub(s.list_visuals(args.page)))
            return
        if args.slicers or args.stub:
            rows = s.list_slicers(args.page)
            if args.stub:
                print(slicer_stub(rows))
            else:
                _print_slicers(rows)
            return
        for v in s.list_visuals(ctx.cfg.page):
            print(f"{v['page']!r:30} {v['type']:20} {v['title']!r:30} {v['name']}")


COMMAND = Command("list", "list pages and visuals (--slicers: slicers)", run, add_arguments)
