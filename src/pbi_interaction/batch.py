"""Run many exports in one logged-in session, with a manifest so batches are resumable."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import time
from pathlib import Path

from .clean import clean_csv
from .runs import Run
from .session import PbiSession

log = logging.getLogger("pbi")

FIELDS = ["label", "status", "rows", "sha256", "seconds", "file", "note", "page", "visual", "filters"]


def _load_manifest(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(newline="") as fh:
        return {row["label"]: row for row in csv.DictReader(fh)}


def _write_manifest(path: Path, rows: dict[str, dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows.values())
    tmp.replace(path)  # rewritten after every run so a crash loses nothing


def run_batch(
    session: PbiSession, runs: list[Run], batch_dir: Path, *, force: bool = False, retries: int = 1,
) -> int:
    """Returns the number of runs that did not finish with status ok."""
    batch_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = batch_dir / "manifest.csv"
    manifest = _load_manifest(manifest_path)
    prev_keys: set = set()
    prev_hash: str | None = None

    for i, run in enumerate(runs, 1):
        if not force and manifest.get(run.label, {}).get("status") == "ok":
            log.info("[%d/%d] skip %s (already ok)", i, len(runs), run.label)
            continue
        log.info("[%d/%d] %s", i, len(runs), run.label)
        row = {
            "label": run.label, "page": run.page, "visual": run.visual,
            "filters": json.dumps([f.to_js()["filter"] for f in run.filters]),
            "status": "error", "rows": "", "sha256": "", "seconds": "", "file": "", "note": "",
        }
        started = time.monotonic()
        for attempt in range(retries + 1):
            try:
                csv_text, keys = session.run_one(run, prev_keys)
                digest = hashlib.sha256(csv_text.encode()).hexdigest()
                # Same bytes as the previous run despite different filters is the classic stale-data symptom.
                if digest == prev_hash and attempt < retries:
                    log.warning("%s: identical to previous run, retrying once", run.label)
                    prev_keys = keys
                    continue
                df = clean_csv(csv_text)
                out = batch_dir / f"{run.label}.csv"
                out.parent.mkdir(parents=True, exist_ok=True)
                df.to_csv(out, index=False)
                suspect = digest == prev_hash
                row.update(
                    status="suspect" if suspect else "ok", rows=len(df), sha256=digest,
                    file=str(out.relative_to(batch_dir)),
                    note="identical to previous run's data despite different filters" if suspect else "",
                )
                prev_keys, prev_hash = keys, digest
                break
            except Exception as e:  # keep going; the failure is recorded and reflected in the exit code
                row["note"] = f"{type(e).__name__}: {e}"[:300]
                log.error("%s attempt %d failed: %s", run.label, attempt + 1, row["note"])
                session._debug_dump(run.label.replace("/", "_"))
        row["seconds"] = round(time.monotonic() - started, 1)
        manifest[run.label] = row
        _write_manifest(manifest_path, manifest)

    bad = [r for r in manifest.values() if r["label"] in {x.label for x in runs} and r["status"] != "ok"]
    for r in bad:
        log.error("%s: %s %s", r["label"], r["status"], r["note"])
    return len(bad)
