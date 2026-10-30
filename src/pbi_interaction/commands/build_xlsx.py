"""`build-xlsx`: build the configured workbooks from an existing batch's CSVs."""

import argparse
from pathlib import Path

from ..batch import remove_csvs
from ..context import Context
from ..runs import expand
from ..settings import RunConfig
from ..workbook import build_workbooks
from .base import Command, add_batch_arg, add_clean_arg, add_only_arg


def build(
    cfg: RunConfig, batch_dir: Path, *, workbook: str | None = None, only: str | None = None, clean: bool = False,
) -> None:
    result = build_workbooks(cfg.workbooks, [r.label for r in expand(cfg)], batch_dir, workbook, only)
    for path in result.paths:
        print(f"wrote {path}")
    if clean:
        remove_csvs(batch_dir, result.consumed)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    add_batch_arg(parser, required=True, help="batch name (folder under the output dir)")
    parser.add_argument("--workbook", help="only build the workbook with this name")
    add_only_arg(parser, help="partial build from runs whose label starts with this prefix, to <name>.partial.xlsx")
    add_clean_arg(parser)


def run(ctx: Context, args: argparse.Namespace) -> None:
    if args.clean and args.only:
        raise ValueError("--clean cannot be combined with --only: CSVs of runs outside the filter would be lost")
    if args.clean and args.workbook:
        raise ValueError("--clean cannot be combined with --workbook: CSVs of unbuilt workbooks would be lost")
    build(ctx.cfg, ctx.settings.out_dir / args.batch, workbook=args.workbook, only=args.only, clean=args.clean)


COMMAND = Command("build-xlsx", "build the configured workbooks from an existing batch's CSVs", run, add_arguments)
