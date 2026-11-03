"""Argument parsing and top-level error handling."""

import argparse
import json
import logging
import signal
import sys
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

from .commands import COMMANDS
from .context import Context
from .errors import PbiError


class JsonFormatter(logging.Formatter):
    """One JSON object per record: ts (ISO 8601, local offset), level, logger, msg and exc when present."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def _configure_logging(args: argparse.Namespace) -> None:
    handler = logging.StreamHandler()
    if args.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=[handler], force=True)


def _raise_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pbi-interaction")
    p.add_argument("--version", action="version", version=f"%(prog)s {version('pbi-interaction')}")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--log-format", choices=["text", "json"], default="text", help="json: one object per log line")
    sub = p.add_subparsers(dest="cmd", required=True)
    for command in COMMANDS:
        sp = sub.add_parser(command.name, help=command.help)
        command.add_arguments(sp)
        sp.set_defaults(run=command.run)
    return p


def _report(args: argparse.Namespace, message: str) -> None:
    if args.log_format == "json":
        logging.getLogger("pbi_interaction").error(message)
    else:
        print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    _configure_logging(args)
    signal.signal(signal.SIGTERM, _raise_interrupt)  # unwind like Ctrl+C so the browser is closed
    ctx = Context(args.config, args.headed)
    try:
        rc = args.run(ctx, args)
    except (FileNotFoundError, ValueError, PbiError) as e:  # ValueError covers config validation errors
        _report(args, f"error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        _report(args, "interrupted")
        sys.exit(130)
    sys.exit(rc or 0)
