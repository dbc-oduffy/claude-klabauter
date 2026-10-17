"""Contract tests for call_io_scope: pinned by open counts, never by timers."""

from __future__ import annotations

import builtins
import os
import sys
import threading

import pytest

from coordinator_core.git import call_io_scope as cis


@pytest.fixture
def opens(monkeypatch):
    real = builtins.open
    seen = []

    def counting(file, *a, **kw):
        seen.append(os.fspath(file))
        return real(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", counting)
    return seen


def _count(opens, p):
    return sum(1 for x in opens if x == os.fspath(p))


def test_scope_reads_once(tmp_path, opens):
    f = tmp_path / "a.txt"
    f.write_bytes(b"hi")
    with cis.call_io_scope():
        assert cis.scope_open()
        for _ in range(5):
            assert cis.read_bytes(f) == b"hi"
    assert _count(opens, f) == 1
    assert not cis.scope_open()


def test_invalidate_path_rereads(tmp_path, opens):
    f = tmp_path / "a.txt"
    f.write_bytes(b"1")
    with cis.call_io_scope():
        cis.read_bytes(f)
        f.write_bytes(b"2")
        assert cis.read_bytes(f) == b"1"
        cis.invalidate(path=f)
        assert cis.read_bytes(f) == b"2"
    assert _count(opens, f) == 2


def test_invalidate_all(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"1")
    with cis.call_io_scope():
        cis.read_bytes(f)
        cis.memo("ns", "k", lambda: 1)
        cis.invalidate()
        f.write_bytes(b"2")
        assert cis.read_bytes(f) == b"2"
        assert cis.memo("ns", "k", lambda: 9) == 9


def test_outside_scope_every_read_opens(tmp_path, opens):
    f = tmp_path / "a.txt"
    f.write_bytes(b"x")
    for _ in range(3):
        assert cis.read_bytes(f) == b"x"
    assert _count(opens, f) == 3
    cis.invalidate(cis.HEAD_NAMESPACE)  # no-op outside a scope


def test_missing_cached_as_none(tmp_path, opens):
    f = tmp_path / "missing"
    with cis.call_io_scope():
        assert cis.read_bytes(f) is None
        f.write_bytes(b"late")
        assert cis.read_bytes(f) is None
    assert _count(opens, f) == 1


def test_permission_error_not_cached(tmp_path, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_bytes(b"x")
    real = builtins.open
    calls = {"n": 0}

    def flaky(file, *a, **kw):
        if os.fspath(file) == os.fspath(f):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("denied")
        return real(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", flaky)
    with cis.call_io_scope():
        with pytest.raises(PermissionError):
            cis.read_bytes(f)
        assert cis.read_bytes(f) == b"x"
    assert calls["n"] == 2


def test_memo_once_per_key():
    n = {"c": 0}

    def compute():
        n["c"] += 1
        return n["c"]

    with cis.call_io_scope():
        assert cis.memo("ns", "k", compute) == 1
        assert cis.memo("ns", "k", compute) == 1
        assert cis.memo("ns", "other", compute) == 2
        assert cis.memo("ns2", "k", compute) == 3
    assert n["c"] == 3


def test_memo_outside_scope_always_computes():
    n = {"c": 0}

    def compute():
        n["c"] += 1
        return n["c"]

    assert cis.memo("ns", "k", compute) == 1
    assert cis.memo("ns", "k", compute) == 2


def test_memo_exception_not_cached():
    n = {"c": 0}

    def compute():
        n["c"] += 1
        if n["c"] == 1:
            raise ValueError("boom")
        return "ok"

    with cis.call_io_scope():
        with pytest.raises(ValueError):
            cis.memo("ns", "k", compute)
        assert cis.memo("ns", "k", compute) == "ok"
        assert cis.memo("ns", "k", compute) == "ok"
    assert n["c"] == 2


def test_invalidate_namespace_only_that_namespace():
    with cis.call_io_scope():
        cis.memo(cis.HEAD_NAMESPACE, "r", lambda: "old")
        cis.memo("other", "r", lambda: "keep")
        cis.invalidate(cis.HEAD_NAMESPACE)
        assert cis.memo(cis.HEAD_NAMESPACE, "r", lambda: "new") == "new"
        assert cis.memo("other", "r", lambda: "changed") == "keep"


def test_sibling_scopes_share_nothing(tmp_path, opens):
    f = tmp_path / "a.txt"
    f.write_bytes(b"x")
    with cis.call_io_scope():
        cis.read_bytes(f)
        cis.memo("ns", "k", lambda: 1)
    with cis.call_io_scope():
        cis.read_bytes(f)
        assert cis.memo("ns", "k", lambda: 2) == 2
    assert _count(opens, f) == 2


def test_nested_scope_shadows(tmp_path, opens):
    f = tmp_path / "a.txt"
    f.write_bytes(b"x")
    with cis.call_io_scope():
        cis.read_bytes(f)
        with cis.call_io_scope():
            cis.read_bytes(f)
        assert cis.scope_open()
        cis.read_bytes(f)
    assert _count(opens, f) == 2


def test_scope_closed_after_exception():
    with pytest.raises(RuntimeError):
        with cis.call_io_scope():
            raise RuntimeError("x")
    assert not cis.scope_open()


@pytest.mark.skipif(
    getattr(sys.flags, "thread_inherit_context", 0),
    reason="threads inherit the context under this flag",
)
def test_plain_thread_does_not_see_scope():
    seen = []
    with cis.call_io_scope():
        t = threading.Thread(target=lambda: seen.append(cis.scope_open()))
        t.start()
        t.join()
    assert seen == [False]


def test_module_imports_only_stdlib():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(cis.__file__).read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    mods.discard("__future__")
    assert mods <= set(sys.stdlib_module_names)
