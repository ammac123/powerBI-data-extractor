"""CLI: `pbi-interaction {probe,list,export}`."""

import argparse
import json
import logging
import sys
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

from pydantic import ValidationError

from .batch import run_batch
from .errors import AuthError, PbiError
from .runs import Run, expand
from .session import PbiSession
from .settings import RunConfig, Settings

__all__ = ["AuthError", "PbiError", "main"]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pbi-interaction")
    p.add_argument("--version", action="version", version=f"%(prog)s {version('pbi-interaction')}")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", help="report SDK version hints and which APIs are available")
    sub.add_parser("list", help="list pages and visuals")
    ex = sub.add_parser("export", help="run every export in config.yaml (the `runs` tree) and save cleaned CSVs")
    ex.add_argument("--batch", help="batch name (output folder); reuse a name to resume it. Default: timestamp")
    ex.add_argument("--only", help="only run labels starting with this prefix, e.g. retail/")
    ex.add_argument("--force", action="store_true", help="rerun runs already marked ok in the manifest")
    ex.add_argument("--dry-run", action="store_true", help="print the expanded runs and exit (no browser)")
    return p


def _load_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]  # populated from env / .env
    except ValidationError as e:
        names = ", ".join(sorted({f"PBI_{err['loc'][0]}".upper() for err in e.errors()}))
        raise PbiError(f"missing or invalid settings: {names}. Set them in the environment or a .env file") from e


def _print_probe(caps: dict, settings: Settings) -> None:
    settings.debug_dir.mkdir(parents=True, exist_ok=True)
    (settings.debug_dir / "capabilities.json").write_text(json.dumps(caps, indent=2))
    print(json.dumps(caps, indent=2))


def _inspect(args: argparse.Namespace, cfg: RunConfig) -> None:
    settings = _load_settings()
    with PbiSession(settings, cfg, headed=args.headed).open() as s:
        if args.cmd == "probe":
            _print_probe(s.probe(), settings)
        else:
            for v in s.list_visuals():
                print(f"{v['page']!r:30} {v['type']:20} {v['title']!r:30} {v['name']}")


def _print_dry_run(runs: list[Run]) -> None:
    for r in runs:
        print(f"{r.label:40} {r.page} / {r.visual}  [{len(r.filters)} filters]")
    print(f"{len(runs)} runs")


def _export(args: argparse.Namespace, cfg: RunConfig) -> None:
    runs = [r for r in expand(cfg) if not args.only or r.label.startswith(args.only)]
    if args.dry_run:
        _print_dry_run(runs)
        return
    settings = _load_settings()
    batch = args.batch or datetime.now().strftime("%Y%m%d_%H%M%S")
    with PbiSession(settings, cfg, headed=args.headed).open() as s:
        failed = run_batch(s, runs, settings.out_dir / batch, force=args.force)
    print(f"{len(runs) - failed}/{len(runs)} ok -> {settings.out_dir / batch}")
    if failed:
        sys.exit(1)


def main() -> None:
    args = _parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    try:
        cfg = RunConfig.load(args.config)
        (_export if args.cmd == "export" else _inspect)(args, cfg)
    except (FileNotFoundError, ValueError, PbiError) as e:  # ValueError covers config validation errors
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
