"""Command definition and shared CLI flags."""

import argparse
from collections.abc import Callable
from dataclasses import dataclass

from ..context import Context


def _no_arguments(parser: argparse.ArgumentParser) -> None:
    pass


@dataclass(frozen=True)
class Command:
    name: str
    help: str
    run: Callable[[Context, argparse.Namespace], int | None]
    add_arguments: Callable[[argparse.ArgumentParser], None] = _no_arguments


def add_batch_arg(parser: argparse.ArgumentParser, *, required: bool = False, help: str) -> None:
    parser.add_argument("--batch", required=required, help=help)


def add_clean_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--clean", action="store_true", help="delete the run CSVs once workbooks are built")


def add_only_arg(parser: argparse.ArgumentParser, *, help: str) -> None:
    parser.add_argument("--only", help=help)
