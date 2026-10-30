import pytest

from pbi_interaction.runs import _merge, expand
from pbi_interaction.settings import FilterSpec, RunConfig


def basic(table, column, *values, **kw):
    return {"table": table, "column": column, "values": list(values), **kw}


def cfg(**kw):
    return RunConfig.model_validate({"page": "P", "visual": "V", **kw})


def test_filters_accumulate_down_tree():
    runs = expand(cfg(
        filters=[basic("Calendar", "Year", 2025)],
        runs=[{"label": "a", "filters": [basic("Product", "Cat", "X")],
               "children": [{"label": "b", "filters": [basic("Ch", "Type", "Online")]}]}],
    ))
    assert [r.label for r in runs] == ["a/b"]
    assert {f.table for f in runs[0].filters} == {"Calendar", "Product", "Ch"}


def test_child_filter_overrides_parent_on_same_target():
    runs = expand(cfg(runs=[{
        "label": "a", "filters": [basic("T", "C", 1)],
        "children": [{"label": "b", "filters": [basic("T", "C", 2)]}],
    }]))
    assert [f.values for f in runs[0].filters] == [[2]]


def test_matrix_is_cartesian_product():
    runs = expand(cfg(runs=[{"label": "m", "matrix": [basic("R", "N", "N", "S"), basic("Y", "Y", 1, 2)]}]))
    assert [r.label for r in runs] == ["m/N_1", "m/N_2", "m/S_1", "m/S_2"]
    assert all(len(r.filters) == 2 and all(len(f.values) == 1 for f in r.filters) for r in runs)


def test_matrix_rejects_raw_filters():
    raw = {"raw": {"target": {"table": "T", "column": "C"}}}
    with pytest.raises(ValueError, match="matrix"):
        expand(cfg(runs=[{"label": "m", "matrix": [raw]}]))


def test_duplicate_labels_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        expand(cfg(runs=[{"label": "a"}, {"label": "a"}]))


@pytest.mark.parametrize("missing", ["page", "visual"])
def test_missing_page_or_visual_rejected(missing):
    base = {"page": "P", "visual": "V", "runs": [{"label": "a"}]}
    del base[missing]
    with pytest.raises(ValueError, match="no page/visual"):
        expand(RunConfig.model_validate(base))


def test_no_runs_yields_single_export():
    assert [r.label for r in expand(cfg())] == ["V"]


def test_page_and_visual_filters_default_to_run_scope():
    runs = expand(cfg(filters=[
        basic("T", "C", 1, level="page"), basic("T", "D", 1, level="visual"), basic("T", "E", 1),
    ]))
    assert set(runs[0].scopes()) == {("page", "P", None), ("visual", "P", "V"), ("report", None, None)}


def test_merge_keeps_distinct_targets_and_replaces_same():
    parent = [FilterSpec(table="T", column="A", values=[1]), FilterSpec(table="T", column="B", values=[1])]
    merged = _merge(parent, [FilterSpec(table="T", column="A", values=[9])])
    assert [(f.column, f.values) for f in merged] == [("A", [9]), ("B", [1])]


def test_merge_distinguishes_levels():
    merged = _merge([FilterSpec(table="T", column="A", values=[1])],
                    [FilterSpec(level="page", page="P", table="T", column="A", values=[2])])
    assert len(merged) == 2


def names():
    return {
        "region": {"id": "r1", "default": [], "values": ["North", "South", "East"]},
        "year": "y1",
        "paged": [{"id": "p_a", "page": "A"}, {"id": "p_b", "page": "B"}],
    }


def test_no_slicers_leaves_runs_unchanged():
    runs = expand(cfg(runs=[{"label": "m", "matrix": [basic("R", "N", "N/x", "S"), basic("Y", "Y", 1)]}]))
    assert [r.label for r in runs] == ["m/N_x_1", "m/S_1"]
    assert all(r.slicers == [] for r in runs)


def test_slicers_inherit_and_child_replaces():
    runs = expand(cfg(
        slicer_names=names(), slicers={"year": 2025, "region": "North"},
        runs=[{"label": "a", "slicers": {"region": ["South", "East"]},
               "children": [{"label": "b", "slicers": {"region": "East"}}, {"label": "c"}]}],
    ))
    by_label = {r.label: {s.ref: s.values for s in r.slicers} for r in runs}
    assert by_label == {
        "a/b": {"year": [2025], "region": ["East"]},
        "a/c": {"year": [2025], "region": ["South", "East"]},
    }
    assert [s.ref for s in runs[0].slicers] == ["year", "region"]


def test_null_slicer_resets_with_registry_default():
    (run,) = expand(cfg(slicer_names=names(), slicers={"region": None}))
    (s,) = run.slicers
    assert s.reset and s.values is None and s.default == [] and s.id == "r1" and s.name == "region"


def test_unknown_slicer_passes_through_unresolved():
    (run,) = expand(cfg(slicers={"Some Title": "x"}))
    s = run.slicers[0]
    assert (s.ref, s.name, s.id, s.default, s.values) == ("Some Title", None, None, None, ["x"])


def test_slicer_matrix_labels_and_mixed_axes():
    runs = expand(cfg(slicer_names=names(), runs=[{"label": "m", "matrix": [
        {"slicer": "region", "values": ["North", "South"]},
        basic("Y", "Y", 2024, 2025),
        {"slicer": "year", "values": ["a/b"]},
    ]}]))
    assert [r.label for r in runs] == ["m/North_2024_a_b", "m/North_2025_a_b", "m/South_2024_a_b", "m/South_2025_a_b"]
    assert [s.values for s in runs[1].slicers] == [["North"], ["a/b"]]
    assert [f.values for f in runs[1].filters] == [[2025]]


def test_slicer_matrix_axis_overrides_inherited_slicer():
    runs = expand(cfg(slicer_names=names(), slicers={"region": "East"},
                      runs=[{"label": "m", "matrix": [{"slicer": "region", "values": ["North", "South"]}]}]))
    assert [r.slicers[0].values for r in runs] == [["North"], ["South"]]


def test_page_scoped_resolution():
    runs = expand(cfg(slicer_names=names(), runs=[
        {"label": "a", "page": "A", "slicers": {"paged": 1}},
        {"label": "b", "page": "B", "slicers": {"paged": 1}},
    ]))
    assert [(r.slicers[0].id, r.slicers[0].page) for r in runs] == [("p_a", "A"), ("p_b", "B")]


def test_page_less_entry_applies_on_any_page():
    (run,) = expand(cfg(slicer_names=names(), slicers={"year": 1}))
    assert (run.slicers[0].id, run.slicers[0].page) == ("y1", None)


def test_named_slicer_without_entry_for_page_rejected():
    with pytest.raises(ValueError, match="no entry for page"):
        expand(cfg(slicer_names=names(), slicers={"paged": 1}))


def test_reset_without_default_rejected():
    with pytest.raises(ValueError, match="cannot reset without a default"):
        expand(cfg(slicer_names=names(), slicers={"year": None}))
    with pytest.raises(ValueError, match="cannot reset without a default"):
        expand(cfg(slicers={"Some Title": None}))


def test_valid_values_enforced_per_page():
    paged = {"paged": [{"id": "a", "page": "A", "values": ["X"]}, {"id": "b", "page": "B", "values": ["Y"]}]}
    config = cfg(slicer_names=paged, runs=[{"label": "r", "page": "B", "slicers": {"paged": "X"}}])
    with pytest.raises(ValueError, match="page 'B'.*'X'"):
        expand(config)


def test_child_null_resets_parent_value_and_parent_null_is_overridden():
    runs = expand(cfg(
        slicer_names=names(), slicers={"region": "North"},
        runs=[{"label": "a", "children": [{"label": "b", "slicers": {"region": None}}]},
              {"label": "c", "slicers": {"region": None}, "children": [{"label": "d", "slicers": {"region": "East"}}]}],
    ))
    by_label = {r.label: (r.slicers[0].reset, r.slicers[0].values) for r in runs}
    assert by_label == {"a/b": (True, None), "c/d": (False, ["East"])}


def test_alias_of_same_slicer_rejected():
    with pytest.raises(ValueError, match="same slicer more than once"):
        expand(cfg(slicer_names=names(), slicers={"region": "North", "r1": "South"}))
