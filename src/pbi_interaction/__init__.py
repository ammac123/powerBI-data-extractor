"""CLI: `pbi-interaction {probe,list,export}`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .batch import run_batch
from .runs import expand
from .session import AuthError, PbiError, PbiSession
from .settings import RunConfig, Settings


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pbi-interaction")
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


def main() -> None:
    args = _parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    cfg = RunConfig.load(args.config)
    runs = []
    if args.cmd == "export":
        runs = [r for r in expand(cfg) if not args.only or r.label.startswith(args.only)]
        if args.dry_run:
            for r in runs:
                print(f"{r.label:40} {r.page} / {r.visual}  [{len(r.filters)} filters]")
            print(f"{len(runs)} runs")
            return

    settings = Settings()  # type: ignore[call-arg]  # populated from env / .env
    try:
        with PbiSession(settings, cfg, headed=args.headed).open() as s:
            if args.cmd == "probe":
                caps = s.probe()
                settings.debug_dir.mkdir(parents=True, exist_ok=True)
                (settings.debug_dir / "capabilities.json").write_text(json.dumps(caps, indent=2))
                print(json.dumps(caps, indent=2))
            elif args.cmd == "list":
                for v in s.list_visuals():
                    print(f"{v['page']!r:30} {v['type']:20} {v['title']!r:30} {v['name']}")
            else:
                batch = args.batch or datetime.now().strftime("%Y%m%d_%H%M%S")
                failed = run_batch(s, runs, settings.out_dir / batch, force=args.force)
                print(f"{len(runs) - failed}/{len(runs)} ok -> {settings.out_dir / batch}")
                if failed:
                    sys.exit(1)
    except (AuthError, PbiError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
