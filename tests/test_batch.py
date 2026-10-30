import csv

import pytest

from pbi_interaction.batch import run_batch
from pbi_interaction.errors import AuthError
from pbi_interaction.runs import Run
from pbi_interaction.session import Export


class FakeSession:
    def __init__(self, script):
        self.script = script  # label -> Export, or an Exception to raise
        self.calls: list[str] = []
        self.dumps: list[str] = []

    def run_one(self, run):
        self.calls.append(run.label)
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


def test_identical_without_render_is_suspect_after_retry(tmp_path):
    same = Export("a\n1\n", rendered=False)
    session = FakeSession({"a": same, "b": same})
    assert run_batch(session, runs("a", "b"), tmp_path) == 1
    assert session.calls == ["a", "b", "b"]
    assert manifest(tmp_path)["b"]["status"] == "suspect"


def test_identical_with_render_is_ok(tmp_path):
    session = FakeSession({"a": data(1), "b": data(1)})
    assert run_batch(session, runs("a", "b"), tmp_path) == 0
    assert "identical" in manifest(tmp_path)["b"]["note"]


def test_previous_hash_resets_after_error(tmp_path):
    same = Export("a\n1\n", rendered=False)
    session = FakeSession({"a": same, "b": RuntimeError("x"), "c": same})
    assert run_batch(session, runs("a", "b", "c"), tmp_path, retries=0) == 1
    assert manifest(tmp_path)["c"]["status"] == "ok"


def test_skipped_runs_do_not_hide_staleness(tmp_path):
    same = Export("a\n1\n", rendered=False)
    run_batch(FakeSession({"a": same}), runs("a"), tmp_path)
    session = FakeSession({"b": same})
    run_batch(session, runs("a", "b"), tmp_path)
    assert manifest(tmp_path)["b"]["status"] == "ok"


def test_auth_error_aborts_batch_and_keeps_completed_runs(tmp_path):
    session = FakeSession({"a": data(1), "b": AuthError("expired"), "c": data(3)})
    with pytest.raises(AuthError):
        run_batch(session, runs("a", "b", "c"), tmp_path)
    assert session.calls == ["a", "b"]
    assert session.dumps == []
    assert {k: v["status"] for k, v in manifest(tmp_path).items()} == {"a": "ok"}
