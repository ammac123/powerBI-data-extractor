# pbi-interaction

Drives a Power BI report embedded in a web page (via Playwright and the page's `powerbi-client` SDK),
applies filters, and exports each visual's data as a cleaned CSV. Many exports run in one logged-in session
and batches are resumable.

## Install

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run playwright install chromium
```

## Credentials

Copy `.env.example` to `.env` and fill it in:

| Variable | Meaning |
| --- | --- |
| `PBI_SITE_URL` | page hosting the embedded report (required) |
| `PBI_USERNAME` | login user (required) |
| `PBI_PASSWORD` | optional; if unset the macOS Keychain is used |
| `PBI_HEADLESS` | `true`/`false` (default `true`) |
| `PBI_TIMEOUT_S` | per-wait timeout in seconds (default `60`) |
| `PBI_STATE_PATH`, `PBI_OUT_DIR`, `PBI_DEBUG_DIR` | where session state, outputs and debug files go |

To keep the password out of `.env`:

```sh
uv run keyring set pbi-interaction you@example.com
```

The logged-in session is cached in `.state/storage_state.json` (mode 600) and reused until it stops working.

## Configuration

```sh
cp config.example.yaml config.yaml   # config.yaml is git-ignored
```

The CLI reads `config.yaml` from the working directory (override with `--config`) and fails if it is missing.

- `embed_wrapper`, `login`: CSS selectors for the embed container and the login form.
- `page`, `visual`, `export_type` (`Summarized` or `Underlying`), `filters`: defaults inherited by every run.
- `hide_others` / `keep_visuals`: hide every other visual on the page so only the target renders.
- `settle_ms`, `stable_reads`: how long the report must be quiet and how many identical consecutive reads
  are needed before data is accepted.
- `runs`: a tree of runs. A node with `children` is a group; a node without is one export. Filters accumulate
  down the tree and a child filter on the same table/column replaces its parent's. `matrix` expands to one
  child per combination of the axes' values. Any node may override `page`, `visual` and `export_type`.

A filter is either `{level, table, column, values}` or `{level, raw: <SDK filter JSON>}`. `level` is
`report` (default), `page` or `visual`; page and visual filters default to the run's own page/visual.

## Usage

```sh
uv run pbi-interaction [--config PATH] [--headed] [-v] [--version] <command>
```

| Command | Purpose |
| --- | --- |
| `probe` | report which SDK APIs the host page offers (also written to `debug/capabilities.json`) |
| `list` | list pages and visuals with their internal names |
| `export` | run every export in the config |

`export` flags:

- `--batch NAME`: output folder name; reuse a name to resume. Default is a timestamp.
- `--only PREFIX`: only runs whose label starts with the prefix, e.g. `retail/`.
- `--force`: rerun runs already marked `ok`.
- `--dry-run`: print the expanded runs without opening a browser.

Exit code is 1 if any run did not finish `ok`.

## Output

```
out/<batch>/
  manifest.csv
  retail/online.csv      # one cleaned CSV per run, path from the label tree
```

`manifest.csv` has one row per run: `label, status, rows, sha256, seconds, file, note, page, visual, filters`.
Status is `ok`, `error`, or `suspect`. On resume, runs already `ok` are skipped; others are rerun.

`suspect` means the data is byte-identical to the previous export and no re-render was observed after the
filters changed, which is the signature of stale data. Identical data after an observed re-render is
accepted as `ok` with a note.

Cleaning strips whitespace and a trailing totals row. Columns whose values are all plain numbers (optionally
with thousands separators) become numeric; anything else, including IDs with leading zeros, stays text.

## Development

```sh
uv run pytest
uv run ruff check .
```
