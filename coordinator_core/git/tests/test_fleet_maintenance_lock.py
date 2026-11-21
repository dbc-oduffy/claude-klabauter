"""Tests for coordinator_core.git.fleet_maintenance_lock: the box-wide pack-deleting-path lock."""

from __future__ import annotations

import os
import textwrap
import time
from pathlib import Path

import pytest

from coordinator_core.git import fleet_maintenance_lock
from coordinator_core.git.fleet_maintenance_lock import FleetLockHomeUnresolved, hold

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


def test_exception_releases_and_resets_depth(box: Path) -> None:
    with pytest.raises(RuntimeError):
        with hold(wait_s=0, holder_label="boom"):
            raise RuntimeError("boom")
    assert fleet_maintenance_lock._depth.__dict__.get("depths") == {}
    with hold(wait_s=0, holder_label="again"):
        pass


def test_lock_target_lives_under_the_settings_home(box: Path) -> None:
    target = fleet_maintenance_lock.lock_target()
    assert box in target.parents


@pytest.fixture()
def unpaused(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.git import pack_deletion_pause

    monkeypatch.setattr(pack_deletion_pause, "PACK_DELETION_PAUSED", False)


def _fake_common(tmp_path: Path) -> Path:
    common = tmp_path / "repo" / ".git"
    (common / "objects" / "pack").mkdir(parents=True)
    (common / "HEAD").write_text("ref: refs/heads/main\n")
    return common


def test_sweep_orphan_pack_files_reaps_when_the_fleet_lock_is_free(box, unpaused, tmp_path: Path) -> None:
    from coordinator_core.git import store_hygiene

    pack_dir = tmp_path / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    rev = pack_dir / f"pack-{'c' * 40}.rev"
    rev.write_bytes(b"x")
    old = time.time() - 3 * store_hygiene.ORPHAN_PACK_AGE_S
    os.utime(rev, (old, old))
    assert store_hygiene.sweep_orphan_pack_files(tmp_path) == 1
    assert not rev.exists()


def test_consolidate_sources_exclude_packs_with_a_git_sibling(tmp_path: Path) -> None:
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


def test_orphan_pack_sweep_reaps_when_the_fleet_lock_is_free(box, unpaused, tmp_path: Path) -> None:
    from coordinator_core.ops import git_maintenance as gm

    pack_dir = _fake_common(tmp_path) / "objects" / "pack"
    orphan = pack_dir / ".tmp-1-pack-abc.pack"
    orphan.write_bytes(b"x")
    old = time.time() - 10 * 3600
    os.utime(orphan, (old, old))
    sweep = gm.sweep_orphan_packs(pack_dir, on_wait=lambda: None)
    assert sweep.deferred is None and sweep.reaped == [orphan]


def test_unresolvable_settings_home_is_a_typed_refusal_naming_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "relative/not-rooted")
    entered = False
    with pytest.raises(FleetLockHomeUnresolved, match="COORDINATOR_SETTINGS_HOME"):
        with hold(wait_s=0, holder_label="x"):
            entered = True
    assert not entered
