"""prune_claude_temp: retention, current-session skip, throttle, tolerance."""

from __future__ import annotations

import os
import time

import pytest

from coordinator_core import temp_layout
from coordinator_core.hooks import sessionstart_async_dispatch as disp
from coordinator_core.ops import prune_claude_temp as pct
from pathlib import Path

DAY = 86400


def _age(path, days) -> None:
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


def test_stale_dir_deleted_and_empty_project_removed(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    d = _session(root, "proj", "s1", 10)
    assert pct.prune(roots=[(root, 2, 7)]) == 1
    assert not d.exists()
    assert not (root / "proj").exists()


def test_fresh_nested_file_keeps_dir(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    d = _session(root, "proj", "s1", 10)
    (d / "sub").mkdir()
    nested = d / "sub" / "new.txt"
    nested.write_text("x")
    _age(d / "sub", 10)
    assert pct.prune(roots=[(root, 2, 7)]) == 0
    assert nested.exists()


def test_current_session_kept_even_if_stale(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    d = _session(root, "proj", "cur", 30)
    assert pct.prune("cur", roots=[(root, 2, 7)]) == 0
    assert d.exists()


def test_fresh_stamp_is_noop(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    d = _session(root, "proj", "s1", 30)
    (root / pct.STAMP_NAME).write_text("")
    assert pct.prune(roots=[(root, 2, 7)]) == 0
    assert d.exists()


def test_old_stamp_runs_and_restamps(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    root.mkdir()
    stamp = root / pct.STAMP_NAME
    stamp.write_text("")
    _age(stamp, 2)
    _session(root, "proj", "s1", 30)
    assert pct.prune(roots=[(root, 2, 7)]) == 1
    assert pct.stamp_is_fresh(root)


def test_missing_root_noop(tmp_path: Path) -> None:
    assert pct.prune(roots=[(tmp_path / "nope", 2, 7)]) == 0


def test_loose_stale_file_under_project_removed(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    (root / "proj").mkdir(parents=True)
    f = root / "proj" / "loose.txt"
    f.write_text("x")
    _age(f, 10)
    _age(root / "proj", 10)
    assert pct.prune(roots=[(root, 2, 7)]) == 1
    assert not (root / "proj").exists()


def test_permission_error_skipped_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_leg_spawns_only_when_due_and_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
def temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(pct.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(temp_layout.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def _file(path, days, text: str = "x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _age(path, days)
    return path


def test_default_roots_include_fleet_and_repo_coordinator_root(temp) -> None:
    repo = temp / "myrepo"
    paths = [r for r, _d, _n in pct.default_roots(str(repo))]
    assert temp_layout.fleet_temp_root() in paths
    assert temp_layout.coordinator_temp_root(repo) in paths


def test_fleet_pass(temp) -> None:
    fleet = temp_layout.fleet_temp_root()
    old = _file(fleet / "guard-unlock-old", 10)
    fresh = _file(fleet / "guard-unlock-new", 1)
    live = _file(fleet / "autonomous-run-live", 30)
    dead = _file(fleet / "autonomous-run-dead", 30)
    pct.prune("live", repo_root="")
    assert not old.exists() and not dead.exists()
    assert fresh.exists() and live.exists()


def test_coordinator_root_skips_pytest_child(temp) -> None:
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


def test_top_level_pass(temp) -> None:
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


def test_top_level_pass_drains_test_leftover_shapes(temp) -> None:
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


def test_top_level_pass_keeps_link(temp) -> None:
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


def test_budget_zero_deletes_one_unit_and_leaves_stamp(temp) -> None:
    fleet = temp_layout.fleet_temp_root()
    a = _file(fleet / "a", 10)
    b = _file(fleet / "b", 10)
    assert pct.prune(budget_seconds=0) == 1
    assert [a.exists(), b.exists()].count(True) == 1
    assert not (fleet / pct.STAMP_NAME).exists()
    assert not pct.stamp_is_fresh(temp_layout.fleet_temp_root().parent)


def test_drain_ignores_budget_and_throttle(temp) -> None:
    fleet = temp_layout.fleet_temp_root()
    files = [_file(fleet / f"f{i}", 10) for i in range(3)]
    (fleet / pct.STAMP_NAME).write_text("")
    legacy = _file(temp / "git-index-3-x", 10)
    parent = fleet.parent
    (parent / pct.STAMP_NAME).write_text("")
    assert pct.prune(budget_seconds=0) == 0
    assert pct.prune(drain=True, budget_seconds=0) == 4
    assert not any(f.exists() for f in files) and not legacy.exists()


def test_main_drain_flag(temp, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    import coordinator_core.ops.cruft_sweep as cs

    monkeypatch.setattr(cs, "box_live_session_ids", lambda: frozenset({"L"}))
    monkeypatch.setattr(pct, "prune", lambda *a, **k: calls.append((a, k)) or 0)
    assert pct.main(["x", "--drain", "sid", "/r"]) == 0
    assert calls == [
        (("sid",), {"repo_root": "/r", "drain": True, "live_session_ids": frozenset({"L"})})
    ]


# --- liveness-keyed owner spaces ---------------------------------------------------------------

GLOB_IGNORE = "scratch/\n.coordinator-local/*\n!.coordinator-local/memo-outbox/\n"


@pytest.fixture(autouse=True)
def _svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from coordinator_core.warm import breadcrumb

    svc = tmp_path / "svc"
    monkeypatch.setattr(breadcrumb, "svc_dir", lambda engine_root=None: svc)
    return svc


def _repo(tmp_path: Path, ignore: str = GLOB_IGNORE, name: str = "repo") -> Path:
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "index").write_bytes(b"DIRC")
    (repo / ".gitignore").write_text(ignore)
    return repo


def _unit(space: Path, name: str, days: float) -> Path:
    d = space / name
    d.mkdir(parents=True)
    _file(d / "f.txt", days)
    _age(d, days)
    return d


def _share(repo: Path) -> Path:
    return repo / ".coordinator-local" / "subagent-share"


def _sidecars(repo: Path) -> Path:
    return repo / ".coordinator-local" / "workflow-sidecars"


def _sessions_live(monkeypatch: pytest.MonkeyPatch, live: "set[str]") -> None:
    from coordinator_core.session import liveness

    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: sid in live)


def _default_prune(temp: Path, repo: Path) -> int:
    """The SessionStart path: no explicit roots, so default_roots decides what is pruned."""
    return pct.prune("current", repo_root=str(repo), live_session_ids=frozenset(), drain=True)


def test_glob_gitignore_line_lists_the_share_root(temp) -> None:
    repo = _repo(temp)
    _share(repo).mkdir(parents=True)
    assert _share(repo) in [r for r, _d, _n in pct.default_roots(str(repo))]


def test_negated_or_absent_gitignore_leaves_the_share_root_unlisted(temp) -> None:
    for i, ignore in enumerate(("scratch/\n", ".coordinator-local/*\n!.coordinator-local/subagent-share/\n")):
        repo = _repo(temp, ignore, name=f"repo{i}")
        _share(repo).mkdir(parents=True)
        assert _share(repo) not in [r for r, _d, _n in pct.default_roots(str(repo))]


def test_dead_owner_48h_unit_deleted_live_owner_7d_unit_kept_on_the_default_path(temp, monkeypatch) -> None:
    repo = _repo(temp)
    dead = _unit(_share(repo), "dead-sess", 2)
    live = _unit(_share(repo), "live-sess", 7)
    _sessions_live(monkeypatch, {"live-sess"})
    assert _default_prune(temp, repo) == 1
    assert not dead.exists() and live.exists()


def test_dead_owner_unit_inside_the_24h_floor_is_kept_without_asking_liveness(temp, monkeypatch) -> None:
    from coordinator_core.session import liveness

    repo = _repo(temp)
    young = _unit(_share(repo), "young-dead", 0.5)
    calls: "list[str]" = []
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: calls.append(sid) or False)
    assert _default_prune(temp, repo) == 0
    assert young.exists() and calls == []


def test_marked_unit_keyed_on_owner_alive_with_unknown_kept(temp, monkeypatch) -> None:
    from coordinator_core import owner_mark

    repo = _repo(temp)
    verdicts = {"gone": False, "running": True, "unknown": None}
    for name in verdicts:
        d = _unit(_sidecars(repo) / "ask", name, 3)
        owner_mark.write_mark(d, owner_mark.OWNER_RUN, name, repo=str(repo))
        _age(d / owner_mark.MARK_FILE, 3)
        _age(d, 3)
    monkeypatch.setattr(owner_mark, "owner_alive", lambda mark: verdicts[mark["owner_id"]])
    monkeypatch.setattr(owner_mark, "is_kept", lambda path, keep=None: False)
    assert _default_prune(temp, repo) == 1
    ask = _sidecars(repo) / "ask"
    assert not (ask / "gone").exists()
    assert (ask / "running").exists() and (ask / "unknown").exists()


def test_marked_unit_inside_its_ttl_is_kept_though_owner_is_dead(temp, monkeypatch) -> None:
    from coordinator_core import owner_mark

    repo = _repo(temp)
    d = _unit(_sidecars(repo) / "ask", "held", 3)
    owner_mark.write_mark(d, owner_mark.OWNER_RUN, "held", repo=str(repo), ttl_hours=24 * 30)
    _age(d, 3)
    monkeypatch.setattr(owner_mark, "owner_alive", lambda mark: False)
    monkeypatch.setattr(owner_mark, "is_kept", lambda path, keep=None: False)
    assert _default_prune(temp, repo) == 0 and d.exists()


def test_keep_set_member_survives_with_a_dead_owner(temp, monkeypatch) -> None:
    from coordinator_core import owner_mark

    repo = _repo(temp)
    kept = _unit(_share(repo), "kept-sess", 30)
    other = _unit(_share(repo), "other-sess", 30)
    _sessions_live(monkeypatch, set())
    monkeypatch.setattr(owner_mark, "is_kept", lambda path, keep=None: Path(path).name == "kept-sess")
    assert _default_prune(temp, repo) == 1
    assert kept.exists() and not other.exists()


def test_unowned_sidecar_unit_waits_for_the_age_bar_and_intermediates_stay(temp, monkeypatch) -> None:
    repo = _repo(temp)
    young = _unit(_sidecars(repo) / "ask", "ask-young", 3)
    old = _unit(_sidecars(repo) / "ask", "ask-old", 9)
    _sessions_live(monkeypatch, set())
    assert _default_prune(temp, repo) == 1
    assert young.exists() and not old.exists()
    old2 = _unit(_sidecars(repo) / "emit", "plan-old", 9)
    assert _default_prune(temp, repo) == 1 and not old2.exists()
    assert (_sidecars(repo) / "emit").is_dir()


def test_owner_space_link_unit_not_followed(temp, monkeypatch) -> None:
    repo = _repo(temp)
    target = _unit(temp / "outside", "t", 30)
    _share(repo).mkdir(parents=True)
    try:
        os.symlink(target, _share(repo) / "lnk", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    _sessions_live(monkeypatch, set())
    _default_prune(temp, repo)
    assert (target / "f.txt").exists()


def test_stall_dumps_dir_joins_default_roots_and_prunes_stale_dumps(temp, _svc: Path) -> None:
    dumps = _svc / pct.STALL_DUMPS_DIRNAME
    old = _file(dumps / "old.txt", 10)
    fresh = _file(dumps / "new.txt", 1)
    assert dumps in [r for r, _d, _n in pct.default_roots("")]
    pct.prune(drain=True, live_session_ids=frozenset())
    assert not old.exists() and fresh.exists()


def test_unit_owner_state_reads_a_tracked_unit_as_kept(temp, monkeypatch) -> None:
    repo = _repo(temp)
    (repo / ".git" / "index").write_bytes(b"DIRC\0.coordinator-local/subagent-share/tracked-sess\0")
    tracked = _unit(_share(repo), "tracked-sess", 30)
    _sessions_live(monkeypatch, set())
    assert _default_prune(temp, repo) == 0 and tracked.exists()


class _FixedBudget(pct._Budget):
    """Latches once `after` children have been checked."""

    after = 3

    def check(self) -> bool:
        self.n = getattr(self, "n", 0) + 1
        if self.limit is not None and self.n >= self.after:
            self.stopped = True
        return self.stopped


def _cursor_root(tmp_path: Path, names, days=10) -> Path:
    root = tmp_path / "fleet"
    root.mkdir()
    for name in names:
        (root / name).write_text("x")
        _age(root / name, days)
    return root


def test_budget_stopped_pass_writes_a_cursor_not_a_stamp_and_the_next_pass_resumes_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = [f"f{i:02d}" for i in range(8)]
    root = _cursor_root(tmp_path, names)
    monkeypatch.setattr(pct, "_Budget", _FixedBudget)
    passes = 0
    seen_cursors = []
    while not (root / pct.STAMP_NAME).exists():
        passes += 1
        assert passes < 20
        pct.prune(roots=[(root, 1, 7)])
        if (root / pct.CURSOR_NAME).exists():
            assert not (root / pct.STAMP_NAME).exists()
            seen_cursors.append((root / pct.CURSOR_NAME).read_text())
    assert passes > 1
    assert seen_cursors == sorted(seen_cursors) and len(set(seen_cursors)) == len(seen_cursors)
    assert [p.name for p in root.iterdir() if p.name.startswith("f")] == []
    assert not (root / pct.CURSOR_NAME).exists()


def test_resumed_pass_starts_after_the_cursor_and_skips_earlier_names(tmp_path: Path) -> None:
    root = _cursor_root(tmp_path, ["a", "b", "c", "d"])
    (root / pct.CURSOR_NAME).write_text("b")
    assert pct.prune(roots=[(root, 1, 7)]) == 2
    assert sorted(p.name for p in root.iterdir() if not p.name.startswith(".")) == ["a", "b"]
    assert not (root / pct.CURSOR_NAME).exists()
    assert (root / pct.STAMP_NAME).exists()


def test_drain_ignores_the_cursor(tmp_path: Path) -> None:
    root = _cursor_root(tmp_path, ["a", "b", "c"])
    (root / pct.CURSOR_NAME).write_text("b")
    assert pct.prune(roots=[(root, 1, 7)], drain=True) == 3
    assert not (root / pct.CURSOR_NAME).exists()


def test_cursor_file_is_never_pruned_as_stale_content(tmp_path: Path) -> None:
    root = _cursor_root(tmp_path, ["a", "z"])
    (root / pct.CURSOR_NAME).write_text("a")
    _age(root / pct.CURSOR_NAME, 30)
    pct.prune(roots=[(root, 1, 7)], budget_seconds=None)
    assert not (root / pct.CURSOR_NAME).exists()
    assert (root / "a").exists() and not (root / "z").exists()
