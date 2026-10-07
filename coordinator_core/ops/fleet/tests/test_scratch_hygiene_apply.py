"""Tests for the ``fleet.scratch_hygiene`` apply executor."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet.scratch_hygiene_apply import apply_purge, remove_tree_no_follow
from coordinator_core.ops.fleet.scratch_hygiene_records import purge_record
from coordinator_core.ops.fleet.tests._scratch_hygiene_fixture import (
    REPO_NAME,
    ScratchHygieneFixture,
    build_two_root_fixture,
)


def _digest(root: Path) -> str:
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            f = Path(dirpath) / name
            h.update(str(f.relative_to(root)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def _planned(fx: ScratchHygieneFixture) -> list[dict]:
    return [
        purge_record(REPO_NAME, p, action, bytes=3, files=1, age_days=10.0, repo_root=fx.repo_root)
        for p, action in fx.expected.items()
    ]


def _always_delete(_path: Path) -> dict:
    return {"action": "would-delete"}


@pytest.fixture
def fx(tmp_path, monkeypatch):
    fixture = build_two_root_fixture(tmp_path, monkeypatch)
    yield fixture
    fixture.close()


@pytest.fixture
def no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)


def test_deleted_set_equals_would_delete_set(fx, no_spawn):
    before = _digest(fx.outside)
    out = apply_purge(fx.repo_root, _planned(fx), recheck=_always_delete)
    deleted = {fx.repo_root / r["path"] if not Path(r["path"]).is_absolute() else Path(r["path"]) for r in out if r["action"] == "deleted"}
    assert deleted == fx.would_delete()
    for p in fx.would_delete():
        assert not os.path.lexists(p)
    for p, action in fx.expected.items():
        if action != "would-delete":
            assert os.path.lexists(p), p
    assert _digest(fx.outside) == before


def test_nested_link_removed_target_identical(fx, no_spawn):
    assert fx.nested_link is not None and fx.top_link is not None
    before = _digest(fx.outside)
    apply_purge(fx.repo_root, _planned(fx), recheck=_always_delete)
    assert not os.path.lexists(fx.nested_link)
    assert os.path.lexists(fx.top_link)
    assert (fx.outside / "keep.txt").read_bytes() == b"must survive"
    assert _digest(fx.outside) == before


def test_skipped_records_pass_through_unchanged(fx):
    planned = _planned(fx)
    out = apply_purge(fx.repo_root, planned, recheck=_always_delete)
    skipped_in = [r for r in planned if r["action"] != "would-delete"]
    assert [r for r in out if r["action"].startswith("skipped-")] == skipped_in


def test_recheck_change_leaves_entry_in_place(fx):
    target = next(iter(fx.would_delete()))

    def recheck(path: Path) -> dict:
        if path == target:
            return {"action": "skipped-young", "reason": "became-young"}
        return {"action": "would-delete"}

    out = apply_purge(fx.repo_root, _planned(fx), recheck=recheck)
    assert target.exists()
    rec = next(r for r in out if r["reason"] == "became-young")
    assert rec["action"] == "skipped-young"
    assert rec["bytes"] == 3 and rec["kind"] == "purge"


def test_path_outside_roots_refused(fx):
    victim = fx.outside / "keep.txt"
    rec = purge_record(REPO_NAME, victim, "would-delete")
    out = apply_purge(fx.repo_root, [rec], recheck=_always_delete)
    assert victim.exists()
    assert out[0]["action"] == "skipped-live" and out[0]["reason"].startswith("refused:")


def test_scratch_hold_and_root_refused(fx):
    recs = [
        purge_record(REPO_NAME, fx.hold_readme, "would-delete", repo_root=fx.repo_root),
        purge_record(REPO_NAME, fx.scratch, "would-delete", repo_root=fx.repo_root),
    ]
    out = apply_purge(fx.repo_root, recs, recheck=_always_delete)
    assert fx.hold_readme.exists() and fx.scratch.exists()
    assert all(r["action"] == "skipped-live" and r["reason"].startswith("refused:") for r in out)


def test_fleet_dir_refused(fx):
    from coordinator_core.temp_layout import fleet_temp_root

    f = fleet_temp_root() / "fleet-posture.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{}")
    out = apply_purge(fx.repo_root, [purge_record(REPO_NAME, f, "would-delete")], recheck=_always_delete)
    assert f.exists()
    assert out[0]["reason"].startswith("refused:")


def test_top_level_link_never_removed_even_if_recheck_says_delete(fx):
    rec = purge_record(REPO_NAME, fx.top_link, "would-delete", repo_root=fx.repo_root)
    before = _digest(fx.outside)
    out = apply_purge(fx.repo_root, [rec], recheck=_always_delete)
    assert out[0]["action"] == "skipped-link"
    assert os.path.lexists(fx.top_link)
    assert _digest(fx.outside) == before
    assert (fx.outside / "keep.txt").exists()


def test_remove_tree_no_follow_on_plain_tree(tmp_path):
    root = tmp_path / "t"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "f").write_bytes(b"x")
    remove_tree_no_follow(root)
    assert not root.exists()


def test_missing_entry_reported_deleted(fx):
    gone = fx.scratch / "never-existed"
    out = apply_purge(fx.repo_root, [purge_record(REPO_NAME, gone, "would-delete", repo_root=fx.repo_root)], recheck=_always_delete)
    assert out[0]["action"] == "deleted" and out[0]["reason"] == "already-gone"
