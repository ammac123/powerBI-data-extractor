from contextlib import contextmanager

import pytest

from pbi_interaction import PbiError, cli, main
from pbi_interaction.commands import COMMANDS, export

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
