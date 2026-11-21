"""Tests for coordinator_core.git.object_store_lock: the shared pack-dir writer lock."""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

import pytest

from coordinator_core.git import object_store_lock
from coordinator_core.git.object_store_lock import HOT_WAIT_S, hold

_REPO_ROOT = str(Path(__file__).resolve().parents[3])

_CHILD_HEADER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from coordinator_core.git.object_store_lock import ObjectStoreBusy, hold
    common = Path(sys.argv[1])
    """
)

_INTERVAL_CHILD = _CHILD_HEADER + textwrap.dedent(
    """
    log, tag, rounds = Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
    for _ in range(rounds):
        with hold(common, wait_s=30.0, holder_label=tag):
            start = time.time_ns()
            time.sleep(0.01)
            end = time.time_ns()
            with open(log, "a") as fh:
                fh.write(f"{start} {end} {tag}\\n")
    """
)

_HOLDER_CHILD = _CHILD_HEADER + textwrap.dedent(
    """
    ready = Path(sys.argv[2])
    with hold(common, wait_s=30.0, holder_label="holder"):
        ready.write_text("ready")
        sys.stdin.read()
    """
)

_TRY_ONCE_CHILD = _CHILD_HEADER + textwrap.dedent(
    """
    try:
        with hold(common, wait_s=0, holder_label="probe"):
            print("acquired")
    except ObjectStoreBusy:
        print("busy")
    """
)


def _plain_repo(root: Path) -> Path:
    pack = root / ".git" / "objects" / "pack"
    pack.mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return root / ".git"


@pytest.fixture()
def common_dir(tmp_path: Path) -> Path:
    return _plain_repo(tmp_path / "repo")


def test_hot_wait_is_inside_the_landing_budget():
    assert 0 < HOT_WAIT_S <= 0.2


def test_exception_releases_and_resets_depth(common_dir: Path):
    with pytest.raises(RuntimeError):
        with hold(common_dir, wait_s=0, holder_label="boom"):
            raise RuntimeError("boom")
    assert object_store_lock._depth.__dict__.get("depths") == {}
    with hold(common_dir, wait_s=0, holder_label="again"):
        pass


def test_keyed_on_common_dir_not_worktree(common_dir: Path, tmp_path: Path):
    other = _plain_repo(tmp_path / "other")
    with hold(common_dir, wait_s=0, holder_label="one"):
        with hold(other, wait_s=0, holder_label="two"):
            pass


def test_uncontended_acquire_release_under_five_ms_process_time(common_dir: Path):
    with hold(common_dir, wait_s=0, holder_label="warm"):
        pass
    rounds = 20
    start = time.process_time()
    for _ in range(rounds):
        with hold(common_dir, wait_s=0, holder_label="timed"):
            pass
    per_cycle_ms = (time.process_time() - start) * 1000 / rounds
    assert per_cycle_ms < 5.0, f"{per_cycle_ms:.2f} ms process time per acquire+release"
