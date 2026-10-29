import pytest
from pydantic import ValidationError

from pbi_interaction.errors import AuthError, PbiError
from pbi_interaction.settings import BASIC_FILTER_TYPE, FilterSpec, RunConfig, Settings


def test_basic_filter_to_sdk():
    f = FilterSpec(table="T", column="C", values=[1, 2]).sdk_filter()
    assert f["target"] == {"table": "T", "column": "C"}
    assert f["operator"] == "In" and f["values"] == [1, 2] and f["filterType"] == BASIC_FILTER_TYPE


def test_raw_filter_passthrough():
    raw = {"target": {"table": "T", "column": "C"}, "operator": "All"}
    assert FilterSpec(raw=raw).sdk_filter() == raw


@pytest.mark.parametrize("kw", [
    {}, {"table": "T", "column": "C"}, {"raw": {"a": 1}, "table": "T"},
    {"level": "report", "page": "P", "table": "T", "column": "C", "values": [1]},
    {"level": "bogus", "table": "T", "column": "C", "values": [1]},
])
def test_invalid_filters(kw):
    with pytest.raises(ValidationError):
        FilterSpec(**kw)


def test_invalid_export_type():
    with pytest.raises(ValidationError):
        RunConfig(export_type="Other")


def test_load_missing_config_errors(tmp_path):
    with pytest.raises(FileNotFoundError, match="config.example.yaml"):
        RunConfig.load(tmp_path / "config.yaml")


def test_load_reads_yaml(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("page: P\nvisual: V\n")
    assert RunConfig.load(path).page == "P"


def test_missing_password_raises_auth_error(monkeypatch):
    monkeypatch.setattr("pbi_interaction.settings.keyring.get_password", lambda *a: None)
    settings = Settings(site_url="https://x", username="u", _env_file=None)
    with pytest.raises(AuthError):
        settings.resolved_password()
    assert issubclass(AuthError, PbiError)
