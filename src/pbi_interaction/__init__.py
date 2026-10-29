"""CLI: `pbi-interaction {probe,list,export}`."""

import argparse
import json
import logging
import sys
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import ValidationError

from .batch import remove_csvs, run_batch
from .errors import AuthError, PbiError
from .runs import Run, expand
from .session import PbiSession
from .settings import RunConfig, Settings
from .workbook import build_workbooks, plan_workbook

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
    ex.add_argument("--format", choices=["csv", "xlsx"], default="csv", help="xlsx also builds the workbooks")
    ex.add_argument("--clean", action="store_true", help="delete the run CSVs once workbooks are built")
    xl = sub.add_parser("build-xlsx", help="build the configured workbooks from an existing batch's CSVs")
    xl.add_argument("--batch", required=True, help="batch name (folder under the output dir)")
    xl.add_argument("--workbook", help="only build the workbook with this name")
    xl.add_argument("--clean", action="store_true", help="delete the run CSVs once workbooks are built")
    return p


def _load_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]  # populated from env / .env
    except ValidationError as e:
        names = ", ".join(sorted({f"PBI_{err['loc'][0]}".upper() for err in e.errors()}))
        raise PbiError(f"missing or invalid settings: {names}. Set them in the environment or a .env file") from e


def _print_probe(caps: Dict, settings: Settings) -> None:
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


def _print_dry_run(runs: List[Run], cfg: RunConfig) -> None:
    for r in runs:
        print(f"{r.label:40} {r.page} / {r.visual}  [{len(r.filters)} filters]")
    print(f"{len(runs)} runs")
    for wb in cfg.workbooks:
        plans = plan_workbook(wb, [r.label for r in expand(cfg)])
        print(f"workbook {wb.name}: " + ", ".join(f"{p.name} ({len(p.labels)})" for p in plans))


def _build(cfg: RunConfig, batch_dir: Path, *, only: Optional[str] = None, clean: bool = False) -> None:
    result = build_workbooks(cfg.workbooks, [r.label for r in expand(cfg)], batch_dir, only)
    for path in result.paths:
        print(f"wrote {path}")
    if clean and not only:
        remove_csvs(batch_dir, result.consumed)


def _export(args: argparse.Namespace, cfg: RunConfig) -> None:
    runs = [r for r in expand(cfg) if not args.only or r.label.startswith(args.only)]
    if args.format == "xlsx" and not cfg.workbooks:
        raise ValueError("--format xlsx needs a `workbooks` section in the config")
    if args.format == "xlsx":
        for spec in cfg.workbooks:
            plan_workbook(spec, [r.label for r in expand(cfg)])
    if args.dry_run:
        _print_dry_run(runs, cfg)
        return
    settings = _load_settings()
    batch = args.batch or datetime.now().strftime("%Y%m%d_%H%M%S")
    with PbiSession(settings, cfg, headed=args.headed).open() as s:
        failed = run_batch(s, runs, settings.out_dir / batch, force=args.force)
    if args.format == "xlsx":
        _build(cfg, settings.out_dir / batch, clean=args.clean and not failed)
    print(f"{len(runs) - failed}/{len(runs)} ok -> {settings.out_dir / batch}")
    if failed:
        sys.exit(1)


def main() -> None:
    args = _parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    try:
        cfg = RunConfig.load(args.config)
        if args.cmd == "build-xlsx":
            _build(cfg, _load_settings().out_dir / args.batch, only=args.workbook, clean=args.clean)
        else:
            (_export if args.cmd == "export" else _inspect)(args, cfg)
    except (FileNotFoundError, ValueError, PbiError) as e:  # ValueError covers config validation errors
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
