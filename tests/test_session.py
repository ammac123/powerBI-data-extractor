from types import SimpleNamespace

import pytest

from pbi_interaction.errors import PbiError
from pbi_interaction.runs import Run
from pbi_interaction.session import PbiSession
from pbi_interaction.settings import FilterSpec, RunConfig


def make_session():
    session = object.__new__(PbiSession)
    session.cfg = RunConfig(keep_visuals=["Keep"])
    session.settings = SimpleNamespace(timeout_s=5)
    session._touched = set()
    session.calls = []
    session.fail_on = None

    def call(fn, *args):
        session.calls.append((fn, args))
        if fn == session.fail_on:
            raise PbiError("boom")
        if fn == "renderCount":
            return 3
        if fn == "exportVisual":
            return {"csv": "a\n1\n", "reads": 2, "rendered": True}

    session.call = call
    return session


def run_with(*filters):
    return Run("r", "P", "V", "Summarized", list(filters))


def report_filter(value):
    return FilterSpec(table="T", column="C", values=[value])


def names(session):
    return [fn for fn, _ in session.calls]


def applied(session):
    return [args[0] for fn, args in session.calls if fn == "applyScopes"]


def test_call_sequence():
    session = make_session()
    export = session.run_one(run_with(report_filter(1)))
    assert names(session) == ["waitReady", "renderCount", "setPage", "hideVisuals", "applyScopes", "exportVisual"]
    assert session.calls[3][1] == ("P", ["V", "Keep"])
    assert session.calls[-1][1][2]["sinceCount"] == 3
    assert export.csv == "a\n1\n" and export.rendered


def test_no_filters_skips_apply_scopes():
    session = make_session()
    session.run_one(run_with())
    assert "applyScopes" not in names(session)


def test_scope_touched_by_previous_run_is_reset():
    session = make_session()
    session.run_one(run_with(report_filter(1)))
    session.run_one(run_with())
    assert applied(session)[1] == [{"level": "report", "page": None, "visual": None, "filters": []}]
    session.run_one(run_with())
    assert len(applied(session)) == 2


def test_scope_reset_after_failed_run():
    session = make_session()
    session.fail_on = "applyScopes"
    with pytest.raises(PbiError):
        session.run_one(run_with(report_filter(1)))
    session.fail_on = None
    session.run_one(run_with())
    assert applied(session)[1] == [{"level": "report", "page": None, "visual": None, "filters": []}]
