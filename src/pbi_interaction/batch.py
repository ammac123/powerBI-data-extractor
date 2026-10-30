"""Run many exports in one logged-in session, with a manifest so batches are resumable."""

import csv
import hashlib
import json
import logging
import time
from collections.abc import Iterable
from pathlib import Path

from .clean import clean_csv
from .errors import AuthError
from .runs import Run
from .session import PbiSession

log = logging.getLogger(__name__)

FIELDS = ["label", "status", "rows", "sha256", "seconds", "file", "note", "page", "visual", "filters"]


def load_manifest(path: Path) -> dict[str, dict]:
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


def _export_run(
    session: PbiSession, run: Run, batch_dir: Path, prev_hash: str | None, retries: int,
) -> tuple[dict, str | None]:
    """Export one run with retries; returns its manifest row and the data hash (None on error)."""
    row = {
        "label": run.label, "page": run.page, "visual": run.visual,
        "filters": json.dumps([f.sdk_filter() for f in run.filters]),
        "status": "error", "rows": "", "sha256": "", "seconds": "", "file": "", "note": "",
    }
    started = time.monotonic()
    digest = None
    for attempt in range(retries + 1):
        try:
            export = session.run_one(run)
            digest = hashlib.sha256(export.csv.encode()).hexdigest()
            # Same bytes as the previous run with no render observed is the classic stale-data symptom.
            # Identical data after an observed render is plausible (e.g. a filter that changes nothing).
            stale = digest == prev_hash and not export.rendered
            if stale and attempt < retries:
                log.warning("%s: identical to previous run with no re-render, retrying", run.label)
                continue
            df = clean_csv(export.csv)
            out = batch_dir / f"{run.label}.csv"
            out.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(out, index=False)
            note = ""
            if digest == prev_hash:
                detail = "with no re-render observed" if stale else "(re-rendered)"
                note = f"identical to previous run's data {detail}"
            row.update(
                status="suspect" if stale else "ok", rows=len(df), sha256=digest,
                file=str(out.relative_to(batch_dir)), note=note,
            )
            break
        except AuthError:
            raise
        except Exception as e:  # keep going; the failure is recorded and reflected in the exit code
            digest = None
            row["note"] = f"{type(e).__name__}: {e}"[:300]
            log.error("%s attempt %d failed: %s", run.label, attempt + 1, row["note"])
            session.debug_dump(run.label.replace("/", "_"))
    row["seconds"] = round(time.monotonic() - started, 1)
    return row, digest


def run_batch(
    session: PbiSession, runs: list[Run], batch_dir: Path, *, force: bool = False, retries: int = 1,
) -> int:
    """Returns the number of runs that did not finish with status ok."""
    batch_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = batch_dir / "manifest.csv"
    manifest = load_manifest(manifest_path)
    # Hash of the data the browser currently shows; unknown (None) after an error. Skipped runs leave it as is.
    prev_hash: str | None = None

    for i, run in enumerate(runs, 1):
        done = manifest.get(run.label, {})
        if not force and done.get("status") == "ok" and (batch_dir / done.get("file", "")).exists():
            log.info("[%d/%d] skip %s (already ok)", i, len(runs), run.label)
            continue
        log.info("[%d/%d] %s", i, len(runs), run.label)
        row, prev_hash = _export_run(session, run, batch_dir, prev_hash, retries)
        manifest[run.label] = row
        _write_manifest(manifest_path, manifest)

    labels = {run.label for run in runs}
    bad = [r for r in manifest.values() if r["label"] in labels and r["status"] != "ok"]
    for r in bad:
        log.error("%s: %s %s", r["label"], r["status"], r["note"])
    return len(bad)


def remove_csvs(batch_dir: Path, labels: Iterable[str]) -> None:
    """Delete the CSVs of the given run labels (and emptied folders); the manifest stays."""
    manifest = load_manifest(batch_dir / "manifest.csv")
    for label in labels:
        if file := manifest.get(label, {}).get("file"):
            (batch_dir / file).unlink(missing_ok=True)
    for folder in sorted((p for p in batch_dir.rglob("*") if p.is_dir()), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()
