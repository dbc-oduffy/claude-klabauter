"""Contract tests for coordinator_core.authoring_leaks: injected detectors only, no git, no spawns."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import types

import pytest

from coordinator_core import authoring_leaks as al
from coordinator_core.authoring_leaks import HeadView, LeakFinding, added_lines, leak_gate


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)


class _Det:
    def __init__(self, name, cands=(), findings=(), exc=None):
        self.name = name
        self._cands = list(cands)
        self._findings = list(findings)
        self._exc = exc
        self.seen_head = None

    def candidates(self, gate_paths):
        return self._cands

    def detect(self, root, gate_paths, head):
        self.seen_head = head
        if self._exc:
            raise self._exc
        return list(self._findings)


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def test_empty_detectors_pass(tmp_path):
    out = leak_gate(tmp_path, ["a.py"], detectors=[])
    assert out.passed and out.diagnostics == []


def test_empty_paths_pass(tmp_path):
    assert leak_gate(tmp_path, [], detectors=[_Det("d")]).passed


def test_injected_finding_refuses_with_format(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "read_tree_spine", lambda *a, **k: {})
    det = _Det("d", findings=[LeakFinding("pkg/a.py", 3, "import_closure", "imports x from y")])
    out = leak_gate(tmp_path, ["pkg\\a.py"], detectors=[det])
    assert not out.passed
    assert out.diagnostics[0] == "leak_gate: pkg/a.py:3: import_closure -- imports x from y"
    assert out.diagnostics[1:] == ["Fix the listed items, or add the missing definer to this commit."]


def test_findings_capped_at_max_rendered(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "read_tree_spine", lambda *a, **k: {})
    extra = 3
    fs = [LeakFinding("a.py", i, "k", "d") for i in range(1, al.MAX_RENDERED_FINDINGS + extra + 1)]
    out = leak_gate(tmp_path, ["a.py"], detectors=[_Det("d", findings=fs)])
    assert sum(1 for d in out.diagnostics if ": k -- " in d) == al.MAX_RENDERED_FINDINGS
    assert f"leak_gate: (+{extra} more)" in out.diagnostics


def test_one_spine_read_for_all_detectors(tmp_path, monkeypatch):
    calls = []

    def fake(repo, paths):
        calls.append(list(paths))
        return {}

    monkeypatch.setattr(al, "read_tree_spine", fake)
    d1 = _Det("d1", cands=["a/x.py", "b\\y.py"])
    d2 = _Det("d2", cands=["b/y.py", "c.py"])
    d3 = _Det("d3")
    assert leak_gate(tmp_path, ["a/x.py"], detectors=[d1, d2, d3]).passed
    assert len(calls) == 1
    assert calls[0] == ["a/x.py", "b/y.py", "c.py"]
    assert d1.seen_head is d2.seen_head is d3.seen_head


def test_no_candidates_no_spine_read(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "read_tree_spine", lambda *a, **k: pytest.fail("read"))
    assert leak_gate(tmp_path, ["a.py"], detectors=[_Det("d")]).passed


def test_raising_detector_refuses_naming_it(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "read_tree_spine", lambda *a, **k: {})
    out = leak_gate(tmp_path, ["a.py"], detectors=[_Det("boomer", exc=ValueError("bad"))])
    assert not out.passed
    assert "boomer" in out.diagnostics[0] and "ValueError" in out.diagnostics[0]


def test_default_detector_import_failure_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "WIRED_DETECTORS", ("no_such_detector_module",))
    out = leak_gate(tmp_path, ["a.py"])
    assert not out.passed
    assert "no_such_detector_module" in out.diagnostics[0]
    assert "failed to import" in out.diagnostics[0]


def test_default_detectors_resolved_by_fixed_module_name(tmp_path, monkeypatch):
    mod = types.ModuleType("coordinator_core.authoring_leaks.fake_det")
    mod.candidates = lambda gp: []
    mod.detect = lambda root, gp, head: [LeakFinding("a.py", 1, "fake", "hit")]
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    monkeypatch.setattr(al, "WIRED_DETECTORS", ("fake_det",))
    out = leak_gate(tmp_path, ["a.py"])
    assert not out.passed and "fake -- hit" in out.diagnostics[0]


def test_added_lines_multiset():
    assert added_lines("a\nb\nc\n", "c\na\nb\n") == []
    assert added_lines("a\nb\n", "a\nb\nb\n") == [(3, "b")]
    assert added_lines("a\n", "a\nx\n") == [(2, "x")]
    assert added_lines(None, "p\nq") == [(1, "p"), (2, "q")]
    assert added_lines("a\nb\n", "a\n") == []


def test_head_view_worktree_differs_and_entries(tmp_path, monkeypatch):
    same, changed = b"same\n", b"new\n"
    spine = {
        "": {"top.py": (0o100644, _blob_sha(same))},
        "pkg": {"mod.py": (0o100644, _blob_sha(b"old\n"))},
    }
    monkeypatch.setattr(al, "read_tree_spine", lambda *a, **k: spine)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "top.py").write_bytes(same)
    (tmp_path / "pkg" / "mod.py").write_bytes(changed)
    (tmp_path / "fresh.py").write_bytes(b"x")
    head = HeadView(tmp_path, ["top.py", "pkg\\mod.py", "fresh.py", "gone.py"])
    assert head.in_head("pkg/mod.py") and head.in_head("pkg\\mod.py")
    assert not head.worktree_differs("top.py")
    assert head.worktree_differs("pkg/mod.py")
    assert head.worktree_differs("fresh.py")
    assert not head.worktree_differs("gone.py")
    (tmp_path / "top.py").unlink()
    assert head.worktree_differs("top.py")


def test_head_view_unresolvable_head_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "read_tree_spine", lambda *a, **k: None)
    head = HeadView(tmp_path, ["a.py"])
    assert not head.in_head("a.py") and head.head_text("a.py") is None


def test_head_text_reads_blob_once(tmp_path, monkeypatch):
    data = "hello\n".encode()
    monkeypatch.setattr(al, "read_tree_spine", lambda *a, **k: {"": {"a.py": (0o100644, _blob_sha(data))}})
    monkeypatch.setattr(al, "resolve_git_common_dir", lambda root: tmp_path)
    reads = []

    def fake_read(common, sha):
        reads.append(sha)
        return ("blob", data)

    monkeypatch.setattr(al, "read_object", fake_read)
    head = HeadView(tmp_path, ["a.py"])
    assert head.head_text("a.py") == "hello\n"
    assert head.head_text("a.py") == "hello\n"
    assert len(reads) == 1


def test_publisher_only_detectors_unwired_without_the_publisher(monkeypatch):
    real = al.importlib.util.find_spec
    monkeypatch.setattr(
        al.importlib.util,
        "find_spec",
        lambda name, *a: None if name == "coordinator_core.percolate" else real(name, *a),
    )
    detectors, failures = al._resolve_default_detectors(al.WIRED_DETECTORS)
    names = {d.__name__.rsplit(".", 1)[-1] for d in detectors}
    assert failures == []
    assert names == set(al.WIRED_DETECTORS) - al.PUBLISHER_ONLY_DETECTORS


def test_publisher_only_detectors_wired_with_the_publisher():
    detectors, failures = al._resolve_default_detectors(al.WIRED_DETECTORS)
    assert failures == []
    assert [d.__name__.rsplit(".", 1)[-1] for d in detectors] == list(al.WIRED_DETECTORS)
