"""prune_claude_temp: retention, current-session skip, throttle, tolerance."""

from __future__ import annotations

import os
import time

import pytest

from coordinator_core import temp_layout
from coordinator_core.hooks import sessionstart_async_dispatch as disp
from coordinator_core.ops import prune_claude_temp as pct

DAY = 86400


def _age(path, days):
    t = time.time() - days * DAY
    os.utime(path, (t, t))


def _session(root, project, session, days, files=("a.txt",)):
    d = root / project / session
    d.mkdir(parents=True)
    for f in files:
        (d / f).write_text("x")
        _age(d / f, days)
    _age(d, days)
    return d


def test_stale_dir_deleted_and_empty_project_removed(tmp_path):
    root = tmp_path / "claude"
    d = _session(root, "proj", "s1", 10)
    assert pct.prune(roots=[(root, 2, 7)]) == 1
    assert not d.exists()
    assert not (root / "proj").exists()


def test_fresh_nested_file_keeps_dir(tmp_path):
    root = tmp_path / "claude"
    d = _session(root, "proj", "s1", 10)
    (d / "sub").mkdir()
    nested = d / "sub" / "new.txt"
    nested.write_text("x")
    _age(d / "sub", 10)
    assert pct.prune(roots=[(root, 2, 7)]) == 0
    assert nested.exists()


def test_current_session_kept_even_if_stale(tmp_path):
    root = tmp_path / "claude"
    d = _session(root, "proj", "cur", 30)
    assert pct.prune("cur", roots=[(root, 2, 7)]) == 0
    assert d.exists()


def test_fresh_stamp_is_noop(tmp_path):
    root = tmp_path / "claude"
    d = _session(root, "proj", "s1", 30)
    (root / pct.STAMP_NAME).write_text("")
    assert pct.prune(roots=[(root, 2, 7)]) == 0
    assert d.exists()


def test_old_stamp_runs_and_restamps(tmp_path):
    root = tmp_path / "claude"
    root.mkdir()
    stamp = root / pct.STAMP_NAME
    stamp.write_text("")
    _age(stamp, 2)
    _session(root, "proj", "s1", 30)
    assert pct.prune(roots=[(root, 2, 7)]) == 1
    assert pct.stamp_is_fresh(root)


def test_missing_root_noop(tmp_path):
    assert pct.prune(roots=[(tmp_path / "nope", 2, 7)]) == 0


def test_loose_stale_file_under_project_removed(tmp_path):
    root = tmp_path / "claude"
    (root / "proj").mkdir(parents=True)
    f = root / "proj" / "loose.txt"
    f.write_text("x")
    _age(f, 10)
    _age(root / "proj", 10)
    assert pct.prune(roots=[(root, 2, 7)]) == 1
    assert not (root / "proj").exists()


def test_permission_error_skipped_not_raised(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    locked = _session(root, "proj", "locked", 10)
    ok = _session(root, "proj", "ok", 10)

    real = pct._rmtree_tolerant

    def fake(path):
        if str(path).endswith("locked"):
            raise PermissionError("locked")
        return real(path)

    monkeypatch.setattr(pct, "_rmtree_tolerant", fake)
    assert pct.prune(roots=[(root, 2, 7)]) == 1
    assert locked.exists() and not ok.exists()


def test_leg_spawns_only_when_due_and_never_raises(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    root.mkdir()
    monkeypatch.setattr(pct, "default_roots", lambda repo_root="": [(root, 2, 7)])
    top = tmp_path / "top"
    top.mkdir()
    (top / pct.STAMP_NAME).write_text("")
    monkeypatch.setattr(pct, "_top_level_stamp_root", lambda: top)
    spawned = []
    monkeypatch.setattr(disp, "spawn_detached", lambda *a, **k: spawned.append(a))
    disp._prune_claude_temp_leg({"payload": {"session_id": "abc"}})
    assert len(spawned) == 1 and spawned[0][2] == ["abc"]
    (root / pct.STAMP_NAME).write_text("")
    disp._prune_claude_temp_leg({"payload": {"session_id": "abc"}})
    assert len(spawned) == 1

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(disp, "spawn_detached", boom)
    (root / pct.STAMP_NAME).unlink()
    assert disp._prune_claude_temp_leg({}) is None


@pytest.fixture
def temp(tmp_path, monkeypatch):
    monkeypatch.setattr(pct.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(temp_layout.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def _file(path, days, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _age(path, days)
    return path


def test_default_roots_include_fleet_and_repo_coordinator_root(temp):
    repo = temp / "myrepo"
    paths = [r for r, _d, _n in pct.default_roots(str(repo))]
    assert temp_layout.fleet_temp_root() in paths
    assert temp_layout.coordinator_temp_root(repo) in paths


def test_fleet_pass(temp):
    fleet = temp_layout.fleet_temp_root()
    old = _file(fleet / "guard-unlock-old", 10)
    fresh = _file(fleet / "guard-unlock-new", 1)
    live = _file(fleet / "autonomous-run-live", 30)
    dead = _file(fleet / "autonomous-run-dead", 30)
    pct.prune("live", repo_root="")
    assert not old.exists() and not dead.exists()
    assert fresh.exists() and live.exists()


def test_coordinator_root_skips_pytest_child(temp):
    repo = temp / "myrepo"
    croot = temp_layout.coordinator_temp_root(repo)
    kept = _file(croot / "pytest" / "run" / "f", 30)
    _age(croot / "pytest" / "run", 30)
    _age(croot / "pytest", 30)
    gone = _file(croot / "run-temp-old" / "f", 30)
    _age(gone.parent, 30)
    pct.prune(repo_root=str(repo))
    assert kept.exists()
    assert not gone.parent.exists()


def test_top_level_pass(temp):
    legacy_old = _file(temp / "git-index-1-abc", 10)
    legacy_new = _file(temp / "git-index-2-new", 1)
    other = _file(temp / "unrelated.txt", 30)
    empty = temp / ".tmpAbC123"
    empty.mkdir()
    _age(empty, 10)
    full = temp / ".tmpXyZ789"
    full.mkdir()
    _file(full / "f", 10)
    _age(full, 10)
    fresh_empty = temp / ".tmpFresh1"
    fresh_empty.mkdir()
    pct.prune()
    assert not legacy_old.exists() and not empty.exists()
    assert legacy_new.exists() and other.exists()
    assert full.exists() and fresh_empty.exists()


def _aged_dir(path, days):
    _file(path / "f", days)
    _age(path, days)
    return path


def test_top_level_pass_drains_test_leftover_shapes(temp):
    old = [
        _aged_dir(temp / n, 10)
        for n in ("altlive-repo-x1", "sgc-cold-e2e-x1", "fmhook-test-x1", "land_pull_test_x1",
                  "claude-klabauter-throwaway-tree-x1", "c3-census-fixture-x1")
    ]
    old_file = _file(temp / "runtime-tripwire-em-dispatch-default-cursor-sid", 10)
    fresh = _aged_dir(temp / "altlive-repo-fresh", 1)
    generic = _aged_dir(temp / "tmpab12cd34", 30)
    pct.prune()
    assert not any(d.exists() for d in old) and not old_file.exists()
    assert fresh.exists() and generic.exists()


def test_top_level_pass_keeps_link(temp):
    target = temp / "target-dir"
    target.mkdir()
    keep = _file(target / "f", 30)
    link = temp / "git-pathspec-4-link.txt"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unavailable")
    pct.prune()
    assert keep.exists() and os.path.lexists(link)


def test_budget_zero_deletes_one_unit_and_leaves_stamp(temp):
    fleet = temp_layout.fleet_temp_root()
    a = _file(fleet / "a", 10)
    b = _file(fleet / "b", 10)
    assert pct.prune(budget_seconds=0) == 1
    assert [a.exists(), b.exists()].count(True) == 1
    assert not (fleet / pct.STAMP_NAME).exists()
    assert not pct.stamp_is_fresh(temp_layout.fleet_temp_root().parent)


def test_drain_ignores_budget_and_throttle(temp):
    fleet = temp_layout.fleet_temp_root()
    files = [_file(fleet / f"f{i}", 10) for i in range(3)]
    (fleet / pct.STAMP_NAME).write_text("")
    legacy = _file(temp / "git-index-3-x", 10)
    parent = fleet.parent
    (parent / pct.STAMP_NAME).write_text("")
    assert pct.prune(budget_seconds=0) == 0
    assert pct.prune(drain=True, budget_seconds=0) == 4
    assert not any(f.exists() for f in files) and not legacy.exists()


def test_main_drain_flag(temp, monkeypatch):
    calls = []
    import coordinator_core.ops.cruft_sweep as cs

    monkeypatch.setattr(cs, "box_live_session_ids", lambda: frozenset({"L"}))
    monkeypatch.setattr(pct, "prune", lambda *a, **k: calls.append((a, k)) or 0)
    assert pct.main(["x", "--drain", "sid", "/r"]) == 0
    assert calls == [
        (("sid",), {"repo_root": "/r", "drain": True, "live_session_ids": frozenset({"L"})})
    ]
