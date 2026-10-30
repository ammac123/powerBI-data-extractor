from contextlib import contextmanager

import pytest
import yaml

from pbi_interaction import PbiError, cli, main
from pbi_interaction.commands import COMMANDS, export
from pbi_interaction.commands.list_ import slicer_stub

CONFIG = """
page: P
visual: V
runs:
  - label: a
  - label: b
workbooks:
  - name: w
    sheets:
      - name: S
        runs: ["a"]
"""

ARGV = {
    "probe": ["probe"],
    "list": ["list"],
    "export": ["export", "--dry-run"],
    "build-xlsx": ["build-xlsx", "--batch", "x"],
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("PBI_SITE_URL", "https://example.test")
    monkeypatch.setenv("PBI_USERNAME", "u")
    monkeypatch.setenv("PBI_OUT_DIR", str(tmp_path / "out"))
    (tmp_path / "config.yaml").write_text(CONFIG)
    return tmp_path


@pytest.fixture
def fake_session(monkeypatch):
    class Fake:
        def __init__(self, *args, **kwargs):
            pass

        @contextmanager
        def open(self):
            yield self

    monkeypatch.setattr("pbi_interaction.context.PbiSession", Fake)


def test_registry_unique_and_complete():
    names = [c.name for c in COMMANDS]
    assert len(set(names)) == len(names)
    assert set(names) == set(ARGV)


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c.name)
def test_parser_maps_each_command(command):
    args = cli.build_parser().parse_args(ARGV[command.name])
    assert args.cmd == command.name
    assert args.run is command.run


def test_export_dry_run_needs_no_browser(env, capsys):
    with pytest.raises(SystemExit) as e:
        main(["export", "--dry-run"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "2 runs" in out and "workbook w" in out


def test_missing_config_exits_1(env, capsys):
    with pytest.raises(SystemExit) as e:
        main(["--config", "nope.yaml", "export", "--dry-run"])
    assert e.value.code == 1
    assert "error: config file not found" in capsys.readouterr().err


def test_pbi_error_exits_1(env, capsys, monkeypatch):
    def boom(ctx, args):
        raise PbiError("boom")

    monkeypatch.setattr(export, "_validate", boom)
    with pytest.raises(SystemExit) as e:
        main(["export"])
    assert e.value.code == 1
    assert "error: boom" in capsys.readouterr().err


def test_failed_batch_exits_1(env, fake_session, monkeypatch, capsys):
    monkeypatch.setattr(export, "run_batch", lambda *a, **k: 1)
    with pytest.raises(SystemExit) as e:
        main(["export"])
    assert e.value.code == 1
    assert "1/2 ok" in capsys.readouterr().out


def test_successful_batch_exits_0(env, fake_session, monkeypatch):
    monkeypatch.setattr(export, "run_batch", lambda *a, **k: 0)
    with pytest.raises(SystemExit) as e:
        main(["export"])
    assert e.value.code == 0


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["export", "--clean"], "--clean needs --format xlsx"),
        (["export", "--clean", "--format", "xlsx", "--only", "a"], "--clean cannot be combined with --only"),
        (["build-xlsx", "--batch", "x", "--clean", "--workbook", "w"], "--clean cannot be combined with --workbook"),
    ],
)
def test_clean_refusals(env, capsys, argv, message):
    with pytest.raises(SystemExit) as e:
        main(argv)
    assert e.value.code == 1
    assert message in capsys.readouterr().err


def test_clean_skipped_when_run_failed(env, fake_session, monkeypatch):
    built = []
    monkeypatch.setattr(export, "run_batch", lambda *a, **k: 1)
    monkeypatch.setattr(export, "build", lambda cfg, batch_dir, **k: built.append(k))
    with pytest.raises(SystemExit):
        main(["export", "--format", "xlsx", "--clean"])
    assert built == [{"only": None, "clean": False}]


def test_build_xlsx_only_wiring(env, monkeypatch):
    seen = []
    monkeypatch.setattr("pbi_interaction.commands.build_xlsx.build", lambda cfg, d, **k: seen.append(k))
    with pytest.raises(SystemExit):
        main(["build-xlsx", "--batch", "x", "--only", "a"])
    assert seen == [{"workbook": None, "only": "a", "clean": False}]


def test_build_xlsx_clean_refused_with_only(env, capsys):
    with pytest.raises(SystemExit):
        main(["build-xlsx", "--batch", "x", "--only", "a", "--clean"])
    assert "--clean cannot be combined with --only" in capsys.readouterr().err


def test_export_dry_run_shows_partial_sheets(env, capsys):
    with pytest.raises(SystemExit):
        main(["export", "--dry-run", "--format", "xlsx", "--only", "b"])
    assert "workbook w.partial: " in capsys.readouterr().out


def _slicer(page="P", title="Region", name="v1", filter_type=1, values=("N",), column="Region"):
    return {
        "page": page, "pageName": page, "name": name, "title": title, "type": "slicer",
        "filterType": filter_type, "targets": [{"table": "T", "column": column}], "values": list(values),
    }  # fmt: skip


def test_slicer_stub_slugs_collisions_and_defaults():
    rows = [
        _slicer(title="Sales Region!", name="a"),
        _slicer(page="Other Page", title="Sales Region!", name="b", values=()),
        _slicer(page="Other Page", title="Sales Region!", name="c"),
        _slicer(title="", name="d", column="Year"),
        _slicer(title="Date", name="e", filter_type=4, values=()),
    ]
    stub = slicer_stub(rows)
    assert "  sales_region: {id: \"a\", page: \"P\", filter_type: 1, default: [\"N\"]}  # " in stub
    assert "'Sales Region!' slicer -> T.Region" in stub
    assert "sales_region_other_page: {id: \"b\", page: \"Other Page\", filter_type: 1, default: []}" in stub
    assert "sales_region_other_page_2: {id: \"c\"" in stub
    assert "year_slicer: {id: \"d\"" in stub
    date = next(line for line in stub.splitlines() if line.strip().startswith("date:"))
    assert "default" not in date.split("#")[0] and "set via raw" in date
    assert yaml.safe_load(stub)["slicer_names"]["sales_region"]["default"] == ["N"]


@pytest.fixture
def slicer_session(monkeypatch):
    calls = []

    class Fake:
        def __init__(self, *args, **kwargs):
            pass

        @contextmanager
        def open(self):
            yield self

        def list_slicers(self, page=None):
            calls.append(("list_slicers", page))
            return [_slicer()]

        def validate_slicers(self, runs):
            calls.append(("validate", len(runs)))

    monkeypatch.setattr("pbi_interaction.context.PbiSession", Fake)
    return calls


def test_list_slicers_output(env, slicer_session, capsys):
    with pytest.raises(SystemExit):
        main(["list", "--slicers", "--page", "P"])
    out = capsys.readouterr().out
    assert "T.Region" in out and "v1" in out and "['N']" in out
    assert slicer_session == [("list_slicers", "P")]


def test_list_stub_output(env, slicer_session, capsys):
    with pytest.raises(SystemExit):
        main(["list", "--stub"])
    assert capsys.readouterr().out.startswith("slicer_names:\n  region: {id: \"v1\"")


SLICER_CONFIG = """
page: P
visual: V
slicer_names:
  region: {id: v1, page: P, default: [All]}
runs:
  - label: a
    slicers: {region: [North, South]}
  - label: b
    slicers: {region: null}
"""


def test_dry_run_shows_slicers(env, capsys):
    (env / "config.yaml").write_text(SLICER_CONFIG)
    with pytest.raises(SystemExit):
        main(["export", "--dry-run"])
    out = capsys.readouterr().out
    assert "[0 filters], 1 slicers: region[v1]=['North', 'South']" in out
    assert "region[v1]=reset(['All'])" in out


def test_dry_run_legacy_config_unchanged(env, capsys):
    with pytest.raises(SystemExit):
        main(["export", "--dry-run"])
    assert "[0 filters]\n" in capsys.readouterr().out


@pytest.mark.parametrize(("config", "expected"), [(SLICER_CONFIG, [("validate", 2)]), (CONFIG, [])])
def test_validate_slicers_only_when_configured(env, slicer_session, monkeypatch, config, expected):
    (env / "config.yaml").write_text(config)
    monkeypatch.setattr(export, "run_batch", lambda *a, **k: 0)
    with pytest.raises(SystemExit):
        main(["export"])
    assert slicer_session == expected


def test_slicer_stub_comments_out_unreadable_slicers():
    row = {**_slicer(name="bad"), "filterType": None, "values": None, "stateError": "no state"}
    stub = slicer_stub([_slicer(name="ok"), row])
    assert list(yaml.safe_load(stub)["slicer_names"]) == ["region"]
    assert "bad on 'P' skipped: no state" in stub
