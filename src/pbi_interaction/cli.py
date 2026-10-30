"""Argument parsing and top-level error handling."""

import argparse
import logging
import sys
from importlib.metadata import version
from pathlib import Path

from .commands import COMMANDS
from .context import Context
from .errors import PbiError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pbi-interaction")
    p.add_argument("--version", action="version", version=f"%(prog)s {version('pbi-interaction')}")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    for command in COMMANDS:
        sp = sub.add_parser(command.name, help=command.help)
        command.add_arguments(sp)
        sp.set_defaults(run=command.run)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    ctx = Context(args.config, args.headed)
    try:
        rc = args.run(ctx, args)
    except (FileNotFoundError, ValueError, PbiError) as e:  # ValueError covers config validation errors
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    sys.exit(rc or 0)
