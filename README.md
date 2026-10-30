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

### Slicers

Slicers can be driven like filters, by friendly name. Generate a starting block with
`list --slicers [--page P]` (one line per slicer: page, title, type, filterType, target, current values, id) or
`list --stub` (a paste-ready `slicer_names:` block with `default` set to the current selection).

```yaml
slicer_names:
  region: {id: 0a1b2c, page: Overview, filter_type: 1, default: [All], values: [North, South, East]}
  year: {id: 3d4e5f, page: Overview, default: [2024]}
slicers:                           # config-wide, or on any run node
  region: [North, South]           # list or scalar values
  year: null                       # restore the default
```

- `slicer_names`: `id` (visual name from `list`), `page`, `filter_type`, `default` (the values `null` restores;
  generated for Basic slicers only), `values` (optional valid values; configured values are checked at config load
  and again per page). A bare string is an id with no default. A name may have one entry per page, or a single
  entry without `page` that matches every page (not both). A slicer ref that is not a name is taken as a raw
  visual id or title.
- `slicers`: dict shorthand `{name: values}`; a dict value is a raw SDK filter. A child node's setting replaces
  its parent's for the same slicer. A `matrix` axis may be `{slicer: region, values: [...]}`.
- `keep_visuals` is the master list: with `hide_others`, everything on the page not listed is hidden. Slicers a
  run sets are always kept visible. Slicer names (or ids) in `keep_visuals` are resolved to their visual ids.
- Kept slicers are synced to `default` (with or without `hide_others`), only updated when their state differs
  from the target. A slicer a run set is reset to its `default` once a later run no longer sets it.
- A slicer without a `default` cannot be reset: `null` is rejected, and setting it is rejected unless every run on
  its page sets it. Setting the same slicer twice in one run (axes, aliases) is rejected.
- A name with only paged entries must have one for each run's page.
- The manifest records each run's slicers; changing them (or a registry id/default) reruns that run on resume.
- Slicers are validated before the first run, so a bad ref fails early. `export --dry-run` lists each run's
  slicers (name, resolved id, values or reset default).

Only Basic slicers (`filter_type` 1) can be set through `values`; other types need `raw`. `filterType` codes:

| Code | Type | Code | Type |
| --- | --- | --- | --- |
| 0 | Advanced | 6 | Tuple |
| 1 | Basic | 7 | RelativeTime |
| 2 | Unknown | 8 | Identity |
| 3 | IncludeExclude | 9 | Hierarchy |
| 4 | RelativeDate | 10 | HierarchyIdentity |
| 5 | TopN | | |

## Usage

```sh
uv run pbi-interaction [--config PATH] [--headed] [-v] [--version] <command>
```

| Command | Purpose |
| --- | --- |
| `probe` | report which SDK APIs the host page offers (also written to `debug/capabilities.json`) |
| `list` | list pages and visuals with their internal names (`--slicers [--page P]` lists slicers, `--stub` prints a `slicer_names` block) |
| `export` | run every export in the config (`--format xlsx` also builds workbooks, `--clean` then deletes the CSVs) |
| `build-xlsx` | build the `workbooks` from an existing batch (`--batch NAME [--workbook NAME] [--only PREFIX] [--clean]`) |

`export` flags:

- `--batch NAME`: output folder name; reuse a name to resume. Default is a timestamp.
- `--only PREFIX`: only runs whose label starts with the prefix, e.g. `retail/`. With `--format xlsx` the
  workbooks are partial (see below).
- `--force`: rerun runs already marked `ok`.
- `--dry-run`: print the expanded runs without opening a browser.

Exit code is 1 if any run did not finish `ok`.

## Workbooks

`export --format xlsx` and `build-xlsx` build Excel workbooks from the `workbooks` config:

```yaml
workbooks:
  - name: sales_report            # -> out/<batch>/sales_report.xlsx
    unassigned: sheet             # runs on no sheet: sheet (own sheet, default) | ignore | error
    sheets:
      - {name: Retail, runs: ["retail/*"], mode: aggregate, key: [Region], layout: merged}
      - {name: Returns, runs: ["returns"]}
```

- `runs`: label globs. A run may be on only one sheet.
- `mode: sheet` (default): one worksheet per matched run. `mode: aggregate`: matched runs share one sheet,
  joined on `key` (required).
- `layout: merged` (default): one table outer-joined on `key`, other columns suffixed `_<run>`; a run with
  repeated key values gets its own sheet. `layout: blocks`: one table per run side by side.
- `_notes` sheet: lists runs that were omitted (failed or not run), flagged `suspect`, or moved to their own sheet.
- `--clean` deletes the CSVs that went into a workbook. It is refused with `--only` or `--workbook`, and is
  skipped if any run failed. Cleaned runs no longer have a CSV, so resuming that batch re-exports them.

Partial builds: with `--only PREFIX` (on `export` or `build-xlsx`) the workbook covers the manifest's runs
whose label matches the prefix, including ones exported in earlier invocations, and is written to
`<name>.partial.xlsx`. It overwrites only other `.partial.xlsx` files, never `<name>.xlsx`. Sheets with no
runs in scope are skipped with a warning, and `_notes` only reports runs in scope. The config is still
validated against all runs, so glob typos fail fast.

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
