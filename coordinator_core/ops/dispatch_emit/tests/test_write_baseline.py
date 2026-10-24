from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import write_baseline as wb


def test_unchanged_dirty_path_is_unwritten(tmp_path):
    (tmp_path / "a.py").write_text("dirty")
    base = wb.capture(tmp_path, ["a.py"])
    assert wb.unwritten(tmp_path, base, ["a.py"]) == ["a.py"]


def test_edited_path_is_not_unwritten(tmp_path):
    (tmp_path / "a.py").write_text("dirty")
    base = wb.capture(tmp_path, ["a.py"])
    (tmp_path / "a.py").write_text("edited")
    assert wb.unwritten(tmp_path, base, ["a.py"]) == []


def test_absent_then_created_is_not_unwritten(tmp_path):
    base = wb.capture(tmp_path, ["new.py"])
    assert base == {"new.py": None}
    assert wb.unwritten(tmp_path, base, ["new.py"]) == ["new.py"]
    (tmp_path / "new.py").write_text("x")
    assert wb.unwritten(tmp_path, base, ["new.py"]) == []


def test_path_missing_from_baseline_is_never_unwritten(tmp_path):
    (tmp_path / "a.py").write_text("x")
    assert wb.unwritten(tmp_path, {}, ["a.py", "gone.py"]) == []


def test_declared_order_and_dedupe(tmp_path):
    (tmp_path / "a").write_text("1")
    (tmp_path / "b").write_text("2")
    base = wb.capture(tmp_path, ["b", "a"])
    assert wb.unwritten(tmp_path, base, ["b", "a", "b"]) == ["b", "a"]


def test_constants():
    assert wb.NO_CHANGE_REFUSAL == "no-change"
    assert wb.NON_REFIRE_REFUSALS == ("unreviewed", "no-change")
    assert wb.MEANT_TO_LAND_KEY == "meant_to_land"
    assert wb.DIRTY_BEFORE_RUN_KEY == "dirty_before_run"


def test_dirt_foreign_path_is_flagged(tmp_path):
    (tmp_path / "a.py").write_text("dirty")
    base = wb.capture(tmp_path, ["a.py"])
    assert wb.pre_existing_dirt(tmp_path, base, ["a.py"]) == ["a.py"]


def test_dirt_absent_path_is_not_flagged(tmp_path):
    base = wb.capture(tmp_path, ["gone.py"])
    assert wb.unwritten(tmp_path, base, ["gone.py"]) == ["gone.py"]
    assert wb.pre_existing_dirt(tmp_path, base, ["gone.py"]) == []


def test_dirt_own_pass_path_is_not_flagged(tmp_path):
    (tmp_path / "a.py").write_text("dirty")
    (tmp_path / "b.py").write_text("dirty")
    base = wb.capture(tmp_path, ["a.py", "b.py"])
    assert wb.pre_existing_dirt(tmp_path, base, ["a.py", "b.py"], own_written=["a.py"]) == ["b.py"]


def test_dirt_edited_path_is_not_flagged(tmp_path):
    (tmp_path / "a.py").write_text("dirty")
    base = wb.capture(tmp_path, ["a.py"])
    (tmp_path / "a.py").write_text("edited")
    assert wb.pre_existing_dirt(tmp_path, base, ["a.py"]) == []


def test_zero_spawns(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    (tmp_path / "a").write_text("1")
    base = wb.capture(tmp_path, ["a", "b"])
    assert wb.unwritten(tmp_path, base, ["a", "b"]) == ["a", "b"]
