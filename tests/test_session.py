from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError

from pbi_interaction.errors import AuthError, PbiError
from pbi_interaction.runs import ResolvedSlicer, Run
from pbi_interaction.session import PbiSession
from pbi_interaction.settings import FilterSpec, RunConfig, SlicerRef


def make_session():
    session = object.__new__(PbiSession)
    session.cfg = RunConfig(keep_visuals=["Keep"])
    session.settings = SimpleNamespace(timeout_s=5)
    session._touched = set()
    session._slicer_applied = {}
    session._slicer_catalog = {}
    session._slicer_set = set()
    session.live = []
    session.apply_override = None
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
        if fn == "listSlicers":
            return session.live
        if fn == "applySlicers":
            items = args[1]
            if session.apply_override:
                return session.apply_override(items)
            return {"applied": [
                {"visual": i["visual"], "ok": True, "values": i["state"].get("values", [])} for i in items
            ]}

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


def live_slicer(name, values, title=None, filter_type=1):
    return {"page": "P", "name": name, "title": title or name, "filterType": filter_type, "values": values}


def slicer_session(keep=("Region", "Category"), **defaults):
    session = make_session()
    session.cfg = RunConfig(
        keep_visuals=list(keep),
        slicer_names={
            "Region": [SlicerRef(id="s_region", default=defaults.get("region", ["EU"]))],
            "Category": [SlicerRef(id="s_cat", default=defaults.get("cat", []))],
        },
    )
    session.live = [live_slicer("s_region", ["EU"]), live_slicer("s_cat", [])]
    return session


def slicer(ref, values=None, **kw):
    sid = {"Region": "s_region", "Category": "s_cat"}.get(ref)
    default = {"Region": ["EU"], "Category": []}.get(ref)
    return ResolvedSlicer(ref, ref if sid else None, sid, None, values, kw.get("raw"), kw.get("reset", False), default)


def slicer_run(*slicers):
    return Run("r", "P", "V", "Summarized", [], list(slicers))


def sent(session):
    return [args[1] for fn, args in session.calls if fn == "applySlicers"]


def test_legacy_config_makes_no_slicer_calls():
    session = make_session()
    session.run_one(run_with())
    assert "listSlicers" not in names(session) and "applySlicers" not in names(session)


def test_slicer_step_order():
    session = slicer_session()
    session.run_one(slicer_run(slicer("Category", ["A"])))
    assert names(session) == [
        "waitReady", "renderCount", "setPage", "listSlicers", "applySlicers", "hideVisuals", "exportVisual",
    ]


def test_only_changed_slicers_are_sent():
    session = slicer_session()
    session.run_one(slicer_run(slicer("Region", ["EU"]), slicer("Category", ["A"])))
    assert sent(session) == [[{"visual": "s_cat", "state": {"kind": "in", "values": ["A"]}}]]


def test_no_apply_when_nothing_differs():
    session = slicer_session()
    session.run_one(slicer_run(slicer("Category", ["A"])))
    session.run_one(slicer_run(slicer("Category", ["A"])))
    assert len(sent(session)) == 1
    assert names(session).count("listSlicers") == 1


def test_kept_unset_slicer_is_restored_to_default():
    session = slicer_session()
    session.run_one(slicer_run(slicer("Category", ["A"])))
    session.run_one(slicer_run())
    assert sent(session)[1] == [{"visual": "s_cat", "state": {"kind": "in", "values": []}}]


def test_reset_uses_registry_default():
    session = slicer_session()
    session.live = [live_slicer("s_region", ["US"]), live_slicer("s_cat", [])]
    session.run_one(slicer_run(slicer("Region", reset=True)))
    assert sent(session) == [[{"visual": "s_region", "state": {"kind": "in", "values": ["EU"]}}]]


def test_never_set_non_kept_slicer_is_left_alone():
    session = slicer_session(keep=("Region",))
    session.run_one(slicer_run())
    session.run_one(slicer_run())
    assert sent(session) == []


def test_non_kept_slicer_set_earlier_is_reset_once_unused():
    session = slicer_session(keep=("Region",))
    session.run_one(slicer_run(slicer("Category", ["A"])))
    session.run_one(slicer_run())
    session.run_one(slicer_run())
    assert sent(session)[1] == [{"visual": "s_cat", "state": {"kind": "in", "values": []}}]
    assert len(sent(session)) == 2


def test_run_set_slicer_stays_visible():
    session = slicer_session(keep=("Region",))
    session.run_one(slicer_run(slicer("Category", ["A"])))
    hide = next(args for fn, args in session.calls if fn == "hideVisuals")
    assert hide == ("P", ["V", "s_region", "s_cat"])


def test_raw_ref_resolves_by_unique_title():
    session = slicer_session(keep=())
    session.live.append(live_slicer("s_x", [], title="Year"))
    session.run_one(slicer_run(ResolvedSlicer("Year", None, None, None, ["2024"], None, False, None)))
    assert sent(session)[0][0]["visual"] == "s_x"


def test_readback_mismatch_raises_and_forces_reread():
    session = slicer_session()
    session.apply_override = lambda items: {"applied": [{"visual": "s_cat", "ok": True, "values": ["B"]}]}
    with pytest.raises(PbiError, match="s_cat.*requested.*A.*got.*B"):
        session.run_one(slicer_run(slicer("Category", ["A"])))
    assert ("P", "s_cat") not in session._slicer_applied
    session.apply_override = None
    session.live = [live_slicer("s_region", ["EU"]), live_slicer("s_cat", ["B"])]
    session.run_one(slicer_run(slicer("Category", ["A"])))
    assert names(session).count("listSlicers") == 2
    assert len(sent(session)) == 2


def test_not_ok_result_raises():
    session = slicer_session()
    session.apply_override = lambda items: {"applied": [{"visual": "s_cat", "ok": False, "error": "nope"}]}
    with pytest.raises(PbiError, match="nope"):
        session.run_one(slicer_run(slicer("Category", ["A"])))
    assert ("P", "s_cat") not in session._slicer_applied


def validation_error(session, *runs):
    with pytest.raises(PbiError) as e:
        session.validate_slicers(list(runs))
    return str(e.value)


def test_validate_passes_on_matching_state():
    session = slicer_session()
    session.validate_slicers([slicer_run(slicer("Category", ["A"]))])
    assert names(session).count("listSlicers") == 1


def test_validate_reports_all_problems():
    session = slicer_session()
    session.live = [live_slicer("s_region", ["US"], filter_type=2)]
    message = validation_error(session, slicer_run(slicer("Region", ["EU"])))
    assert "'Category' (s_cat) not found" in message
    assert "filterType 2" in message
    assert "not a Basic slicer" in message


def test_validate_default_mismatch():
    session = slicer_session()
    session.live = [live_slicer("s_region", ["US"]), live_slicer("s_cat", [])]
    assert "expected default ['EU']" in validation_error(session, slicer_run())


def test_validate_raw_ref_unknown_and_ambiguous():
    session = slicer_session(keep=())
    session.live += [live_slicer("a", [], title="Year"), live_slicer("b", [], title="Year")]
    raw = lambda ref: ResolvedSlicer(ref, None, None, None, ["1"], None, False, None)  # noqa: E731
    message = validation_error(session, slicer_run(raw("Year"), raw("Nope")))
    assert "'Year' is ambiguous; candidates: Year [a], Year [b]" in message
    assert "'Nope' matches no slicer" in message


def test_unknown_live_values_are_not_an_empty_selection():
    session = slicer_session()
    session.live = [live_slicer("s_region", ["EU"]), live_slicer("s_cat", None, filter_type=5)]
    session.run_one(slicer_run(slicer("Category", ["A"])))
    session.live = [live_slicer("s_region", ["EU"]), live_slicer("s_cat", None, filter_type=5)]
    session.apply_override = lambda items: {"applied": [{"visual": "s_cat", "ok": True, "values": None}]}
    session.run_one(slicer_run(slicer("Category", raw={"filters": []})))
    session.apply_override = None
    session.run_one(slicer_run())
    assert [i["state"] for i in sent(session)[-1]] == [{"kind": "in", "values": []}]


def test_slicer_without_default_must_be_set_by_every_run():
    session = slicer_session(keep=())
    raw = ResolvedSlicer("Year", None, None, None, ["1"], None, False, None)
    session.live.append(live_slicer("s_x", [], title="Year"))
    session.validate_slicers([slicer_run(raw), slicer_run(raw)])
    message = validation_error(session, slicer_run(raw), slicer_run())
    assert "'Year' has no default to reset to but is not set by every run" in message


def test_apply_invalidates_other_pages_cache():
    session = slicer_session()
    session._slicer_applied[("Q", "s_other")] = ("x",)
    session._slicer_catalog["Q"] = [live_slicer("s_other", ["x"])]
    session.run_one(slicer_run(slicer("Category", ["A"])))
    assert ("Q", "s_other") not in session._slicer_applied and "Q" not in session._slicer_catalog


def test_validate_reports_unreadable_state():
    session = slicer_session()
    unreadable = {**live_slicer("s_cat", None, filter_type=None), "stateError": "no page"}
    session.live = [live_slicer("s_region", ["EU"]), unreadable]
    assert "'Category' state is unreadable: no page" in validation_error(session, slicer_run())


class FakeLocator:
    def __init__(self, present=False, visible=False):
        self.present, self.visible = present, visible

    def or_(self, other):
        return FakeOr(self, other)

    def count(self):
        return int(self.present)

    def is_visible(self):
        return self.visible


class FakeOr:
    def __init__(self, *locators):
        self.first = self
        self.locators = locators

    def wait_for(self, **kwargs):
        if not any(loc.present or loc.visible for loc in self.locators):
            raise PlaywrightError("Timeout")


def auth_session(tmp_path, embed, login):
    session = make_session()
    state = tmp_path / "state.json"
    state.write_text("{}")
    session.settings = SimpleNamespace(timeout_s=5, state_path=state, site_url="http://x")
    session.page = SimpleNamespace(
        goto=lambda url: None, locator=lambda sel: embed if sel == session.cfg.embed_wrapper else login
    )
    session.logged_in = False
    session._login = lambda: setattr(session, "logged_in", True)
    return session, state


def test_saved_session_reused_when_embed_appears(tmp_path):
    session, state = auth_session(tmp_path, FakeLocator(present=True), FakeLocator())
    session._ensure_logged_in()
    assert state.exists() and not session.logged_in


def test_saved_session_dropped_when_login_form_appears(tmp_path):
    session, state = auth_session(tmp_path, FakeLocator(), FakeLocator(visible=True))
    session._ensure_logged_in()
    assert not state.exists() and session.logged_in


def test_saved_session_kept_when_nothing_appears(tmp_path):
    session, state = auth_session(tmp_path, FakeLocator(), FakeLocator())
    with pytest.raises(AuthError):
        session._ensure_logged_in()
    assert state.exists() and not session.logged_in


def test_login_waits_for_report_before_saving_state(tmp_path):
    session = make_session()
    state = tmp_path / "sub" / "state.json"
    order = []
    password = SimpleNamespace(get_secret_value=lambda: "p")
    session.settings = SimpleNamespace(timeout_s=5, state_path=state, username="u", resolved_password=lambda: password)
    session.page = SimpleNamespace(fill=lambda *a: None, click=lambda *a: None)
    session._embed_present = lambda timeout: True
    session.wait_ready = lambda: order.append("wait_ready")
    session._context = SimpleNamespace(storage_state=lambda path: (order.append("save"), open(path, "w").close()))
    session._login()
    assert order == ["wait_ready", "save"]


def test_call_configures_wrapper_before_every_call():
    session = make_session()
    sent = []
    session.page = SimpleNamespace(evaluate=lambda js, arg: sent.append((js, arg)))
    del session.call  # use the real bridge
    session.call("probe")
    assert sent[0][1] == [{"wrapper": session.cfg.embed_wrapper}, "probe", []]
    assert "configure(cfg)" in sent[0][0]

