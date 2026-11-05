"""Tests for coordinator_core.launch_records."""

import glob
import os
import threading

import pytest

from coordinator_core import launch_records as lr


def test_paths_and_unsafe_ids(tmp_path):
    t = str(tmp_path)
    assert lr.session_dir(t, "s1") == os.path.join(t, "workflow-runs", "s1")
    p = lr.record_path(t, "s1", "t1")
    assert p == os.path.join(t, "workflow-runs", "s1", "t1.json")
    assert os.path.isdir(lr.session_dir(t, "s1"))
    assert os.path.basename(lr.flat_record_path(t, "s1", "t1")) == "workflow-run-s1-t1.json"
    for bad in ("../x", "a/b", "a\\b", "", "a b"):
        with pytest.raises(ValueError):
            lr.session_dir(t, bad)
        with pytest.raises(ValueError):
            lr.record_path(t, "s1", bad)
        with pytest.raises(ValueError):
            lr.flat_record_path(t, bad, "t1")


def test_session_record_paths_absent_and_present(tmp_path):
    t = str(tmp_path)
    assert lr.session_record_paths(t, "s1") == []
    assert lr.session_record_paths(t, "../bad") == []
    p = lr.record_path(t, "s1", "t1")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{}")
    assert lr.session_record_paths(t, "s1") == [p]
    assert lr.session_record_paths(t, "s2") == []


def _forbid_listing(monkeypatch, forbidden):
    real_scandir, real_listdir, real_glob = os.scandir, os.listdir, glob.glob
    owner = threading.get_ident()

    def norm(x):
        return os.path.normcase(os.path.abspath(os.fspath(x)))

    def guard(real):
        def wrapped(path=".", *a, **k):
            if threading.get_ident() == owner and norm(path) in forbidden:
                raise AssertionError(f"listed {path}")
            return real(path, *a, **k)
        return wrapped

    monkeypatch.setattr(os, "scandir", guard(real_scandir))
    monkeypatch.setattr(os, "listdir", guard(real_listdir))

    def g(pattern, *a, **k):
        if threading.get_ident() == owner and norm(os.path.dirname(pattern)) in forbidden:
            raise AssertionError(f"globbed {pattern}")
        return real_glob(pattern, *a, **k)

    monkeypatch.setattr(glob, "glob", g)


def test_session_record_paths_never_lists_parent(tmp_path, monkeypatch):
    t = str(tmp_path)
    lr.record_path(t, "s1", "t1")
    open(lr.record_path(t, "s1", "t1"), "w", encoding="utf-8").close()
    forbidden = {
        os.path.normcase(os.path.abspath(t)),
        os.path.normcase(os.path.abspath(os.path.join(t, "workflow-runs"))),
    }
    with monkeypatch.context() as scoped:
        _forbid_listing(scoped, forbidden)
        assert len(lr.session_record_paths(t, "s1")) == 1


def test_migrate_moves_named_flat_without_listing(tmp_path, monkeypatch):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    ta, tb = str(a), str(b)
    src1 = lr.flat_record_path(ta, "s1", "t1")
    src2 = lr.flat_record_path(tb, "s1", "t2")
    for s, c in ((src1, "one"), (src2, "two")):
        with open(s, "w", encoding="utf-8") as f:
            f.write(c)
    unrelated = a / "workflow-run-s1-notnamed.json"
    unrelated.write_text("x")
    forbidden = {os.path.normcase(os.path.abspath(d)) for d in (ta, tb)}
    forbidden.add(os.path.normcase(os.path.abspath(os.path.join(ta, "workflow-runs"))))
    forbidden.add(os.path.normcase(os.path.abspath(lr.session_dir(ta, "s1"))))
    with monkeypatch.context() as scoped:
        _forbid_listing(scoped, forbidden)
        n = lr.migrate_flat_records([ta, tb], "s1", ["t1", "t2", "t3", "t1", "../bad"])
    assert n == 2
    assert open(os.path.join(ta, "workflow-runs", "s1", "t1.json"), encoding="utf-8").read() == "one"
    assert open(os.path.join(ta, "workflow-runs", "s1", "t2.json"), encoding="utf-8").read() == "two"
    assert not os.path.exists(src1) and not os.path.exists(src2)
    assert unrelated.exists()
    assert not os.path.exists(lr.session_dir(ta, "s1") + "/t3.json")


def test_migrate_never_overwrites_and_creates_no_dir_without_source(tmp_path):
    t = str(tmp_path)
    assert lr.migrate_flat_records([t], "s1", ["t1"]) == 0
    assert not os.path.exists(lr.session_dir(t, "s1"))
    dest = lr.record_path(t, "s1", "t1")
    with open(dest, "w", encoding="utf-8") as f:
        f.write("new")
    src = lr.flat_record_path(t, "s1", "t1")
    with open(src, "w", encoding="utf-8") as f:
        f.write("old")
    assert lr.migrate_flat_records([t], "s1", ["t1"]) == 0
    assert open(dest, encoding="utf-8").read() == "new"
    assert lr.migrate_flat_records([t], "bad/s", ["t1"]) == 0
