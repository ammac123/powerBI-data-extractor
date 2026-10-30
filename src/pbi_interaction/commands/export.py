"""`export`: run every export in the config's `runs` tree and save cleaned CSVs (optionally workbooks)."""

import argparse
from datetime import datetime

from ..batch import run_batch
from ..context import Context
from ..runs import Run, expand
from ..settings import RunConfig
from ..workbook import plan_workbook
from .base import Command, add_batch_arg, add_clean_arg, add_only_arg
from .build_xlsx import build


def add_arguments(parser: argparse.ArgumentParser) -> None:
    add_batch_arg(parser, help="batch name (output folder); reuse a name to resume it. Default: timestamp")
    add_only_arg(parser, help="only run labels starting with this prefix, e.g. retail/; xlsx is then partial")
    parser.add_argument("--force", action="store_true", help="rerun runs already marked ok in the manifest")
    parser.add_argument("--dry-run", action="store_true", help="print the expanded runs and exit (no browser)")
    parser.add_argument("--format", choices=["csv", "xlsx"], default="csv", help="xlsx also builds the workbooks")
    add_clean_arg(parser)


def _validate(args: argparse.Namespace, cfg: RunConfig) -> None:
    if args.clean and args.format != "xlsx":
        raise ValueError("--clean needs --format xlsx")
    if args.clean and args.only:
        raise ValueError("--clean cannot be combined with --only: CSVs of runs outside the filter would be lost")
    if args.format == "xlsx" and not cfg.workbooks:
        raise ValueError("--format xlsx needs a `workbooks` section in the config")
    if args.format == "xlsx":
        for spec in cfg.workbooks:
            plan_workbook(spec, [r.label for r in expand(cfg)])


def _print_dry_run(runs: list[Run], cfg: RunConfig, only: str | None) -> None:
    for r in runs:
        print(f"{r.label:40} {r.page} / {r.visual}  [{len(r.filters)} filters]")
    print(f"{len(runs)} runs")
    for wb in cfg.workbooks:
        plans = plan_workbook(wb, [r.label for r in runs], partial=only is not None)
        suffix = ".partial" if only else ""
        print(f"workbook {wb.name}{suffix}: " + ", ".join(f"{p.name} ({len(p.labels)})" for p in plans))


def run(ctx: Context, args: argparse.Namespace) -> int:
    cfg = ctx.cfg
    _validate(args, cfg)
    runs = [r for r in expand(cfg) if not args.only or r.label.startswith(args.only)]
    if args.dry_run:
        _print_dry_run(runs, cfg, args.only)
        return 0
    batch_dir = ctx.settings.out_dir / (args.batch or datetime.now().strftime("%Y%m%d_%H%M%S"))
    with ctx.session() as s:
        failed = run_batch(s, runs, batch_dir, force=args.force)
    if args.format == "xlsx":
        build(cfg, batch_dir, only=args.only, clean=args.clean and not failed)
    print(f"{len(runs) - failed}/{len(runs)} ok -> {batch_dir}")
    return 1 if failed else 0


COMMAND = Command("export", "run every export in the config `runs` tree and save cleaned CSVs", run, add_arguments)
