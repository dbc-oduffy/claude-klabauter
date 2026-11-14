"""Tests for coordinator_core.git.fleet_maintenance_lock: the box-wide pack-deleting-path lock."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from coordinator_core.git import fleet_maintenance_lock
from coordinator_core.git.fleet_maintenance_lock import (
    FleetLockHomeUnresolved,
    FleetMaintenanceBusy,
    hold,
)

_REPO_ROOT = str(Path(__file__).resolve().parents[3])

_HOLDER_CHILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from coordinator_core.git.fleet_maintenance_lock import hold
    ready = Path(sys.argv[1])
    with hold(wait_s=30.0, holder_label="holder"):
        ready.write_text("ready")
        sys.stdin.read()
    """
)

_TRY_ONCE_CHILD = textwrap.dedent(
    """
    from coordinator_core.git.fleet_maintenance_lock import FleetMaintenanceBusy, hold
    try:
        with hold(wait_s=0, holder_label="probe"):
            print("acquired")
    except FleetMaintenanceBusy:
        print("busy")
    """
)


@pytest.fixture()
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated settings home and lock rendezvous; children inherit both."""
    home = tmp_path / "settings-home"
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_LOCK_ROOT", str(tmp_path / "lock-root"))
    return home


def _spawn(script: str, *args: str, cwd: Path, stdin=subprocess.DEVNULL) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        env=env,
        cwd=str(cwd),
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for(path: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.02)


def test_second_process_is_busy_for_two_different_repos(box: Path, tmp_path: Path):
    repos = [tmp_path / "repo-a", tmp_path / "repo-b"]
    for repo in repos:
        repo.mkdir()
    ready = tmp_path / "ready"
    holder = _spawn(_HOLDER_CHILD, str(ready), cwd=repos[0], stdin=subprocess.PIPE)
    try:
        _wait_for(ready)
        for repo in repos:
            probe = _spawn(_TRY_ONCE_CHILD, cwd=repo)
            out, err = probe.communicate(timeout=60)
            assert out.strip() == "busy", err
        entered = False
        with pytest.raises(FleetMaintenanceBusy):
            with hold(wait_s=0, holder_label="contender"):
                entered = True
        assert not entered
    finally:
        holder.stdin.close()
        holder.communicate(timeout=60)

    probe = _spawn(_TRY_ONCE_CHILD, cwd=repos[1])
    out, err = probe.communicate(timeout=60)
    assert out.strip() == "acquired", err


def test_reentrant_within_one_process_and_depth_resets(box: Path, tmp_path: Path):
    with hold(wait_s=0, holder_label="outer"):
        with hold(wait_s=0, holder_label="inner"):
            pass
        probe = _spawn(_TRY_ONCE_CHILD, cwd=tmp_path)
        out, err = probe.communicate(timeout=60)
        assert out.strip() == "busy", "inner exit must not release the outer hold"
    assert fleet_maintenance_lock._depth.__dict__.get("depths") == {}


def test_exception_releases_and_resets_depth(box: Path):
    with pytest.raises(RuntimeError):
        with hold(wait_s=0, holder_label="boom"):
            raise RuntimeError("boom")
    assert fleet_maintenance_lock._depth.__dict__.get("depths") == {}
    with hold(wait_s=0, holder_label="again"):
        pass


def test_lock_target_lives_under_the_settings_home(box: Path):
    target = fleet_maintenance_lock.lock_target()
    assert box in target.parents


@pytest.fixture()
def other_process_holds(box: Path, tmp_path: Path):
    """The fleet lock held by a second process for the duration of the test."""
    ready = tmp_path / "holder-ready"
    holder = _spawn(_HOLDER_CHILD, str(ready), cwd=tmp_path, stdin=subprocess.PIPE)
    try:
        _wait_for(ready)
        yield
    finally:
        holder.stdin.close()
        holder.communicate(timeout=60)


@pytest.fixture()
def unpaused(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.git import pack_deletion_pause

    monkeypatch.setattr(pack_deletion_pause, "PACK_DELETION_PAUSED", False)


def _pack_files(pack_dir: Path) -> set[str]:
    return {p.name for p in pack_dir.iterdir()}


def _fake_common(tmp_path: Path) -> Path:
    common = tmp_path / "repo" / ".git"
    (common / "objects" / "pack").mkdir(parents=True)
    (common / "HEAD").write_text("ref: refs/heads/main\n")
    return common


def _blob_pending(tag: bytes) -> dict:
    import hashlib

    return {hashlib.sha1(b"blob %d\0" % len(tag) + tag).hexdigest(): (b"blob", tag)}


def test_fold_defers_while_another_process_holds_the_fleet_lock(box, unpaused, tmp_path):
    from coordinator_core.git import git_objects as go

    common = _fake_common(tmp_path)
    go._flush_pack(common, _blob_pending(b"first"))
    pack_dir = common / "objects" / "pack"
    before = _pack_files(pack_dir)
    marker = (pack_dir / "claude-klabauter-tail").read_bytes()

    holder = _spawn(_HOLDER_CHILD, str(tmp_path / "ready"), cwd=tmp_path, stdin=subprocess.PIPE)
    try:
        _wait_for(tmp_path / "ready")
        go._flush_pack(common, _blob_pending(b"second"))
    finally:
        holder.stdin.close()
        holder.communicate(timeout=60)
    assert before <= _pack_files(pack_dir), "a pack was deleted while the fleet lock was held elsewhere"
    assert (pack_dir / "claude-klabauter-tail").read_bytes() == marker
    assert len(_pack_files(pack_dir) - before) == 2, "the deferred flush must still land its own pack"

    go._flush_pack(common, _blob_pending(b"third"))
    assert not before <= _pack_files(pack_dir), "free of the fleet lock the fold deletes the old tail"


def test_sweep_orphan_pack_files_defers_while_another_process_holds_the_fleet_lock(
    box, unpaused, other_process_holds, tmp_path
):
    from coordinator_core.git import store_hygiene

    pack_dir = tmp_path / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    rev = pack_dir / f"pack-{'c' * 40}.rev"
    rev.write_bytes(b"x")
    old = time.time() - 3 * store_hygiene.ORPHAN_PACK_AGE_S
    os.utime(rev, (old, old))
    assert store_hygiene.sweep_orphan_pack_files(tmp_path) == 0
    assert rev.exists()


def test_sweep_orphan_pack_files_reaps_when_the_fleet_lock_is_free(box, unpaused, tmp_path):
    from coordinator_core.git import store_hygiene

    pack_dir = tmp_path / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    rev = pack_dir / f"pack-{'c' * 40}.rev"
    rev.write_bytes(b"x")
    old = time.time() - 3 * store_hygiene.ORPHAN_PACK_AGE_S
    os.utime(rev, (old, old))
    assert store_hygiene.sweep_orphan_pack_files(tmp_path) == 1
    assert not rev.exists()


def test_consolidate_pack_batch_defers_while_another_process_holds_the_fleet_lock(
    box, unpaused, other_process_holds, tmp_path
):
    from coordinator_core.git import store_hygiene

    pack_dir = tmp_path / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    for i in range(store_hygiene.CONSOLIDATE_MAX_PACKS + 8):
        (pack_dir / f"pack-{i:040x}.idx").write_bytes(b"x" * 64)
        (pack_dir / f"pack-{i:040x}.pack").write_bytes(b"x" * 64)
    before = _pack_files(pack_dir)
    assert store_hygiene.consolidate_pack_batch(tmp_path) == 0
    assert _pack_files(pack_dir) == before


def test_consolidate_sources_exclude_packs_with_a_git_sibling(tmp_path):
    from coordinator_core.git import store_hygiene

    pack_dir = tmp_path / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    for i, ext in enumerate((None, ".rev", ".keep", ".promisor", ".mtimes", ".bitmap")):
        stem = f"pack-{i:040x}"
        (pack_dir / f"{stem}.idx").write_bytes(b"x")
        (pack_dir / f"{stem}.pack").write_bytes(b"x")
        if ext:
            (pack_dir / f"{stem}{ext}").write_bytes(b"x")
    sources = [idx.name for idx, _ in store_hygiene._small_packs(pack_dir)]
    assert sources == [f"pack-{0:040x}.idx"]


def _init_repo(path: Path) -> Path:
    (path / ".git" / "objects" / "pack").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return path


def test_run_tier_defers_while_another_process_holds_the_fleet_lock(
    box, unpaused, other_process_holds, tmp_path, monkeypatch
):
    from coordinator_core.ops import git_maintenance as gm

    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(gm, "run_git", lambda *a, **k: pytest.fail("a deferred tier spawned git"))
    for tier in gm.TIERS:
        result = gm.run_tier(repo, tier)
        assert result.deferred and "fleet" in result.deferred
        assert not result.ran


def test_orphan_pack_sweep_defers_while_another_process_holds_the_fleet_lock(
    box, unpaused, other_process_holds, tmp_path
):
    from coordinator_core.ops import git_maintenance as gm

    pack_dir = _fake_common(tmp_path) / "objects" / "pack"
    orphan = pack_dir / ".tmp-1-pack-abc.pack"
    orphan.write_bytes(b"x")
    old = time.time() - 10 * 3600
    os.utime(orphan, (old, old))
    sweep = gm.sweep_orphan_packs(pack_dir, on_wait=lambda: None)
    assert sweep.deferred and sweep.reaped == []
    assert orphan.exists()


def test_orphan_pack_sweep_reaps_when_the_fleet_lock_is_free(box, unpaused, tmp_path):
    from coordinator_core.ops import git_maintenance as gm

    pack_dir = _fake_common(tmp_path) / "objects" / "pack"
    orphan = pack_dir / ".tmp-1-pack-abc.pack"
    orphan.write_bytes(b"x")
    old = time.time() - 10 * 3600
    os.utime(orphan, (old, old))
    sweep = gm.sweep_orphan_packs(pack_dir, on_wait=lambda: None)
    assert sweep.deferred is None and sweep.reaped == [orphan]


def test_tick_child_defers_every_tier_while_another_process_holds_the_fleet_lock(
    box, unpaused, other_process_holds, tmp_path, monkeypatch
):
    from coordinator_core.git import maintenance_tick, store_hygiene
    from coordinator_core.ops import git_maintenance as gm

    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(store_hygiene, "_box_busy", lambda: pytest.fail("a deferred child sampled the box"))
    monkeypatch.setattr(gm, "run_tier", lambda *a, **k: pytest.fail("a deferred child ran a tier"))
    assert maintenance_tick.run_due(repo, ["daily", "weekly"]) == 0
    common = repo / ".git"
    for tier in maintenance_tick.TIERS:
        age = time.time() - maintenance_tick.stamp_path(common, tier).stat().st_mtime
        assert age >= maintenance_tick._INTERVALS[tier] - maintenance_tick.RETRY_S - 60, "stamp not backdated"


def test_unresolvable_settings_home_is_a_typed_refusal_naming_the_key(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "relative/not-rooted")
    entered = False
    with pytest.raises(FleetLockHomeUnresolved, match="COORDINATOR_SETTINGS_HOME"):
        with hold(wait_s=0, holder_label="x"):
            entered = True
    assert not entered
