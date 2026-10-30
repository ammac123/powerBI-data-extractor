import csv
import json
from types import SimpleNamespace

import pytest

from pbi_interaction.batch import run_batch
from pbi_interaction.errors import AuthError
from pbi_interaction.runs import ResolvedSlicer, Run
from pbi_interaction.session import Export


class FakeSession:
    def __init__(self, script):
        self.script = script  # label -> Export, or an Exception to raise
        self.calls: list[str] = []
        self.dumps: list[str] = []
        self.deadlines: list[float] = []
        self.settings = SimpleNamespace(run_timeout_s=300)

    def run_one(self, run, deadline):
        self.calls.append(run.label)
        self.deadlines.append(deadline)
        result = self.script[run.label]
        if isinstance(result, Exception):
            raise result
        return result

    def debug_dump(self, label):
        self.dumps.append(label)


def runs(*labels):
    return [Run(label, "P", "V", "Summarized", []) for label in labels]


def manifest(batch_dir):
    with (batch_dir / "manifest.csv").open() as fh:
        return {r["label"]: r for r in csv.DictReader(fh)}


def data(n):
    return Export(f"a\n{n}\n", rendered=True)


def test_all_ok_writes_files_and_manifest(tmp_path):
    session = FakeSession({"x": data(1), "g/y": data(2)})
    assert run_batch(session, runs("x", "g/y"), tmp_path) == 0
    assert (tmp_path / "g" / "y.csv").exists()
    assert {k: v["status"] for k, v in manifest(tmp_path).items()} == {"x": "ok", "g/y": "ok"}


def test_resume_skips_ok_and_reruns_errors(tmp_path):
    first = FakeSession({"a": data(1), "b": RuntimeError("boom")})
    assert run_batch(first, runs("a", "b"), tmp_path, retries=0) == 1
    assert manifest(tmp_path)["b"]["status"] == "error"
    assert first.dumps == ["b"]

    second = FakeSession({"a": data(1), "b": data(2)})
    assert run_batch(second, runs("a", "b"), tmp_path) == 0
    assert second.calls == ["b"]
    assert manifest(tmp_path)["b"]["status"] == "ok"


def test_force_reruns_ok(tmp_path):
    run_batch(FakeSession({"a": data(1)}), runs("a"), tmp_path)
    again = FakeSession({"a": data(1)})
    run_batch(again, runs("a"), tmp_path, force=True)
    assert again.calls == ["a"]


def test_identical_data_is_ok_with_a_note(tmp_path):
    unchanged = Export("a\n1\n", rendered=False)
    session = FakeSession({"a": data(1), "b": data(1), "c": unchanged})
    assert run_batch(session, runs("a", "b", "c"), tmp_path) == 0
    assert session.calls == ["a", "b", "c"]
    notes = {k: v["note"] for k, v in manifest(tmp_path).items()}
    assert notes == {"a": "", "b": "identical to previous run's data (re-rendered)",
                     "c": "identical to previous run's data (nothing changed)"}


def test_retries_share_one_deadline(tmp_path):
    class FlakySession(FakeSession):
        def run_one(self, run, deadline):
            super().run_one(run, deadline)
            if len(self.calls) == 1:
                raise RuntimeError("transient")
            return data(1)

    session = FlakySession({"a": None})
    assert run_batch(session, runs("a"), tmp_path) == 0
    assert len(session.deadlines) == 2 and len(set(session.deadlines)) == 1


def test_auth_error_aborts_batch_and_keeps_completed_runs(tmp_path):
    session = FakeSession({"a": data(1), "b": AuthError("expired"), "c": data(3)})
    with pytest.raises(AuthError):
        run_batch(session, runs("a", "b", "c"), tmp_path)
    assert session.calls == ["a", "b"]
    assert session.dumps == []
    assert {k: v["status"] for k, v in manifest(tmp_path).items()} == {"a": "ok"}


def slicer_runs(values):
    slicer = ResolvedSlicer("Region", "Region", "s1", None, values, None, False, None)
    return [Run("a", "P", "V", "Summarized", [], [slicer])]


def test_manifest_records_slicers(tmp_path):
    reset = ResolvedSlicer("Cat", "Cat", "s2", None, None, None, True, [])
    raw = ResolvedSlicer("Y", None, None, None, None, {"k": 1}, False, None)
    runs_ = [Run("a", "P", "V", "Summarized", [], [reset, raw, *slicer_runs(["EU"])[0].slicers])]
    run_batch(FakeSession({"a": data(1)}), runs_, tmp_path)
    assert json.loads(manifest(tmp_path)["a"]["slicers"]) == {
        "Cat": ["s2", [], "default"], "Y": [None, None, {"k": 1}], "Region": ["s1", None, ["EU"]],
    }
    run_batch(FakeSession({"b": data(1)}), runs("b"), tmp_path)
    assert manifest(tmp_path)["b"]["slicers"] == ""


def test_changed_slicers_rerun_but_same_skip(tmp_path):
    run_batch(FakeSession({"a": data(1)}), slicer_runs(["EU"]), tmp_path)
    same = FakeSession({"a": data(1)})
    run_batch(same, slicer_runs(["EU"]), tmp_path)
    assert same.calls == []
    changed = FakeSession({"a": data(2)})
    run_batch(changed, slicer_runs(["US"]), tmp_path)
    assert changed.calls == ["a"]


def test_old_manifest_without_slicers_column_resumes(tmp_path):
    (tmp_path / "a.csv").write_text("a\n1\n")
    old_header = "label,status,rows,sha256,seconds,file,note,page,visual,filters"
    (tmp_path / "manifest.csv").write_text(f"{old_header}\na,ok,1,h,1,a.csv,,P,V,[]\n")
    session = FakeSession({"a": data(1)})
    assert run_batch(session, runs("a"), tmp_path) == 0
    assert session.calls == []


def test_changed_registry_default_reruns(tmp_path):
    def reset_run(default):
        reset = ResolvedSlicer("Cat", "Cat", "s2", None, None, None, True, default)
        return [Run("a", "P", "V", "Summarized", [], [reset])]

    run_batch(FakeSession({"a": data(1)}), reset_run([]), tmp_path)
    changed = FakeSession({"a": data(1)})
    run_batch(changed, reset_run(["All"]), tmp_path)
    assert changed.calls == ["a"]
