"""`list`: list pages and visuals."""

import argparse

from ..context import Context
from .base import Command


def run(ctx: Context, args: argparse.Namespace) -> None:
    with ctx.session() as s:
        for v in s.list_visuals():
            print(f"{v['page']!r:30} {v['type']:20} {v['title']!r:30} {v['name']}")


COMMAND = Command("list", "list pages and visuals", run)
