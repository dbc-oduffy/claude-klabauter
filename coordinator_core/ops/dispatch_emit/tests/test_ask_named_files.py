"""Pins named_test_files: node-id file parts and unique bare test-module stems."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_named_files
from coordinator_core.ops.dispatch_emit.ask_named_files import named_test_files
from coordinator_core.ops.dispatch_emit.tests._git_fixture import _git, seed_repo


def _repo(tmp_path):
    root = seed_repo(tmp_path / "repo")
    for rel in ("a/b/test_x.py", "pkg1/test_dup.py", "pkg2/test_dup.py", "top/test_one.py", "test_root.py"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
    (root / "untracked").mkdir()
    (root / "untracked/test_new.py").write_text("x\n", encoding="utf-8")
    _git(["add", "a", "pkg1", "pkg2", "top", "test_root.py"], root)
    _git(["commit", "-q", "-m", "files"], root)
    return root


def test_node_id_returns_file_part(tmp_path):
    root = _repo(tmp_path)
    assert named_test_files(["fix a/b/test_x.py::test_y"], root) == ["a/b/test_x.py"]


def test_node_id_with_class_and_trailing_punct(tmp_path):
    root = _repo(tmp_path)
    assert named_test_files(["`a/b/test_x.py::TestK::test_y`,"], root) == ["a/b/test_x.py"]


def test_node_id_of_absent_file_names_nothing(tmp_path):
    root = _repo(tmp_path)
    assert named_test_files(["nodir/test_gone.py::test_y"], root) == []


def test_bare_unique_stem_resolves(tmp_path):
    root = _repo(tmp_path)
    assert named_test_files(["the 4 red tests in test_one and test_root."], root) == [
        "top/test_one.py",
        "test_root.py",
    ]


def test_ambiguous_stem_names_nothing(tmp_path):
    root = _repo(tmp_path)
    assert named_test_files(["test_dup"], root) == []


def test_untracked_stem_names_nothing(tmp_path):
    root = _repo(tmp_path)
    assert named_test_files(["test_new test_missing"], root) == []


def test_dedupes_across_forms_and_texts(tmp_path):
    root = _repo(tmp_path)
    got = named_test_files(["a/b/test_x.py::test_y", "test_x", "top/test_one.py::t", "test_one"], root)
    assert got == ["a/b/test_x.py", "top/test_one.py"]


def test_stems_cost_one_git_spawn(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    calls = []
    real = ask_named_files.run_git

    def counting(args, **kw):
        calls.append(args)
        return real(args, **kw)

    monkeypatch.setattr(ask_named_files, "run_git", counting)
    named_test_files(["test_x test_one test_root test_dup test_new"], root)
    assert len(calls) == 1


def test_no_stem_means_no_spawn(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(ask_named_files, "run_git", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawn")))
    assert named_test_files(["a/b/test_x.py::test_y plain words"], root) == ["a/b/test_x.py"]
