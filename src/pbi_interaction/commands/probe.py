"""`probe`: report SDK version hints and which APIs are available."""

import argparse
import json

from ..context import Context
from .base import Command


def run(ctx: Context, args: argparse.Namespace) -> None:
    with ctx.session() as s:
        caps = s.probe()
    ctx.settings.debug_dir.mkdir(parents=True, exist_ok=True)
    (ctx.settings.debug_dir / "capabilities.json").write_text(json.dumps(caps, indent=2))
    print(json.dumps(caps, indent=2))


COMMAND = Command("probe", "report SDK version hints and which APIs are available", run)
