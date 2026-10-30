import pytest
from pydantic import ValidationError

from pbi_interaction.errors import AuthError, PbiError
from pbi_interaction.settings import BASIC_FILTER_TYPE, FilterSpec, RunConfig, Settings, SlicerRef, SlicerSpec


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


@pytest.mark.parametrize("source", ["env", "dotenv"])
def test_empty_password_falls_back_to_keyring(source, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PBI_SITE_URL", "https://x")
    monkeypatch.setenv("PBI_USERNAME", "u")
    if source == "env":
        monkeypatch.setenv("PBI_PASSWORD", "")
    else:
        monkeypatch.delenv("PBI_PASSWORD", raising=False)
        (tmp_path / ".env").write_text("PBI_PASSWORD=\n")
    monkeypatch.setattr("pbi_interaction.settings.keyring.get_password", lambda *a: "from-keyring")
    assert Settings().resolved_password().get_secret_value() == "from-keyring"


def slicer_cfg(**kw):
    return RunConfig.model_validate(kw)


def test_slicer_names_normalisation():
    c = slicer_cfg(slicer_names={
        "year": "y1",
        "region": {"id": "r1", "page": "Overview", "default": [], "values": ["N"]},
        "paged": [{"id": "a", "page": "A"}, {"id": "b", "page": "B"}],
    })
    assert c.slicer_names["year"] == [SlicerRef(id="y1")]
    assert c.slicer_names["region"][0].page == "Overview" and c.slicer_names["region"][0].filter_type == 1
    assert [r.id for r in c.slicer_names["paged"]] == ["a", "b"]


def test_slicers_shorthand():
    c = slicer_cfg(slicers={"a": 1, "b": ["x", "y"], "c": None, "d": {"k": 1}})
    assert [(s.slicer, s.values, s.raw, s.reset) for s in c.slicers] == [
        ("a", [1], None, False), ("b", ["x", "y"], None, False), ("c", None, None, True), ("d", None, {"k": 1}, False),
    ]


def test_run_node_slicers_and_matrix_discrimination():
    (node,) = slicer_cfg(runs=[{"label": "r", "slicers": {"a": 1}, "matrix": [
        {"slicer": "s", "values": [1, 1, 2]},
        {"table": "T", "column": "C", "values": [1]},
    ]}]).runs
    assert node.slicers[0].values == [1]
    assert isinstance(node.matrix[0], SlicerSpec) and node.matrix[0].values == [1, 2]
    assert isinstance(node.matrix[1], FilterSpec)


@pytest.mark.parametrize("kw", [
    {"slicer": "s"},
    {"slicer": "s", "values": []},
    {"slicer": "s", "values": [1], "reset": True},
    {"slicer": "s", "values": [1], "raw": {}},
])
def test_invalid_slicer_specs(kw):
    with pytest.raises(ValidationError):
        SlicerSpec(**kw)


@pytest.mark.parametrize("axis", [{"slicer": "s", "raw": {"a": 1}}, {"slicer": "s", "reset": True}])
def test_slicer_matrix_axis_needs_values(axis):
    with pytest.raises(ValidationError, match="needs values"):
        slicer_cfg(runs=[{"label": "r", "matrix": [axis]}])


NAMES = {"region": {"id": "r1", "values": ["North", 2025]}}


@pytest.mark.parametrize("where", [
    {"slicers": {"region": "West"}},
    {"runs": [{"label": "r", "slicers": {"region": ["North", "West"]}}]},
    {"runs": [{"label": "r", "children": [{"label": "c", "matrix": [{"slicer": "region", "values": ["West"]}]}]}]},
])
def test_values_enforced_against_valid_values(where):
    with pytest.raises(ValidationError, match="region.*West"):
        slicer_cfg(slicer_names=NAMES, **where)


def test_valid_values_compared_loosely_and_unknown_names_allowed():
    slicer_cfg(slicer_names=NAMES, slicers={"region": ["2025", "North"], "Unregistered": "anything"})


@pytest.mark.parametrize("names, match", [
    ({"a": [{"id": "1", "page": "P"}, {"id": "2", "page": "P"}]}, "same page"),
    ({"a": [{"id": "1"}, {"id": "2", "page": "P"}]}, "without a page"),
    ({"a": {"id": "1", "page": "P"}, "b": {"id": "1", "page": "P"}}, "share page and id"),
])
def test_invalid_slicer_names(names, match):
    with pytest.raises(ValidationError, match=match):
        slicer_cfg(slicer_names=names)


def test_same_id_on_different_pages_allowed():
    slicer_cfg(slicer_names={"a": {"id": "1", "page": "P"}, "b": {"id": "1", "page": "Q"}})


def test_non_basic_slicer_rejects_values_but_allows_raw_and_reset():
    names = {"d": {"id": "1", "filter_type": 2}}
    with pytest.raises(ValidationError, match="Basic"):
        slicer_cfg(slicer_names=names, slicers={"d": "x"})
    slicer_cfg(slicer_names=names, slicers={"d": None})
    slicer_cfg(slicer_names=names, slicers={"d": {"filterType": 2}})


def test_duplicate_matrix_axes_on_same_slicer_rejected():
    axes = [{"slicer": "s", "values": [1]}, {"slicer": "s", "values": [2]}]
    with pytest.raises(ValidationError, match="same slicer"):
        slicer_cfg(runs=[{"label": "r", "matrix": axes}])


def test_unhashable_and_mixed_type_matrix_values_are_deduped_safely():
    axis = {"slicer": "s", "values": [[1, 2], [1, 2], 1, 1.0, True]}
    (node,) = slicer_cfg(runs=[{"label": "r", "matrix": [axis]}]).runs
    assert node.matrix[0].values == [[1, 2], 1, 1.0, True]


@pytest.mark.parametrize("names, match", [
    ({"a": []}, "at least one entry"),
    ({"a": {"id": "1", "values": []}}, "empty valid values"),
])
def test_empty_registry_entries_rejected(names, match):
    with pytest.raises(ValidationError, match=match):
        slicer_cfg(slicer_names=names)


def test_axis_with_slicer_and_filter_keys_rejected():
    with pytest.raises(ValidationError):
        slicer_cfg(runs=[{"label": "r", "matrix": [{"slicer": "s", "table": "T", "column": "C", "values": [1]}]}])
