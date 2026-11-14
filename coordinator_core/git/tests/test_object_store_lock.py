"""Tests for coordinator_core.git.object_store_lock: the shared pack-dir writer lock."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from coordinator_core.git import object_store_lock
from coordinator_core.git.object_store_lock import HOT_WAIT_S, ObjectStoreBusy, hold

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


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _spawn(script: str, *args: str, stdin=subprocess.DEVNULL) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        env=_child_env(),
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _plain_repo(root: Path) -> Path:
    pack = root / ".git" / "objects" / "pack"
    pack.mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return root / ".git"


@pytest.fixture()
def common_dir(tmp_path: Path) -> Path:
    return _plain_repo(tmp_path / "repo")


def _wait_for(path: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.02)


def test_hot_wait_is_inside_the_landing_budget():
    assert 0 < HOT_WAIT_S <= 0.2


def test_two_real_processes_never_overlap(common_dir: Path, tmp_path: Path):
    log = tmp_path / "intervals.log"
    procs = [_spawn(_INTERVAL_CHILD, str(common_dir), str(log), tag, "5") for tag in ("a", "b")]
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err

    rows = sorted(
        (int(s), int(e), tag)
        for s, e, tag in (line.split() for line in log.read_text().splitlines())
    )
    assert len(rows) == 10
    assert {tag for _, _, tag in rows} == {"a", "b"}
    for (_, prev_end, _), (next_start, _, _) in zip(rows, rows[1:]):
        assert prev_end <= next_start, "two holders were inside the lock at once"


def test_held_elsewhere_raises_busy_within_wait(common_dir: Path, tmp_path: Path):
    ready = tmp_path / "ready"
    holder = _spawn(_HOLDER_CHILD, str(common_dir), str(ready), stdin=subprocess.PIPE)
    try:
        _wait_for(ready)
        entered = False
        started = time.monotonic()
        with pytest.raises(ObjectStoreBusy):
            with hold(common_dir, wait_s=0.1, holder_label="contender"):
                entered = True
        assert not entered
        assert time.monotonic() - started < 5.0

        with pytest.raises(ObjectStoreBusy):
            with hold(common_dir, wait_s=0, holder_label="contender"):
                entered = True
        assert not entered
    finally:
        holder.stdin.close()
        holder.communicate(timeout=60)

    with hold(common_dir, wait_s=5.0, holder_label="after-release"):
        pass


def test_released_lock_is_free_for_another_process(common_dir: Path):
    with hold(common_dir, wait_s=0, holder_label="outer"):
        probe = _spawn(_TRY_ONCE_CHILD, str(common_dir))
        out, err = probe.communicate(timeout=60)
        assert out.strip() == "busy", err
    probe = _spawn(_TRY_ONCE_CHILD, str(common_dir))
    out, err = probe.communicate(timeout=60)
    assert out.strip() == "acquired", err


def test_reentrant_within_one_process(common_dir: Path):
    with hold(common_dir, wait_s=0, holder_label="outer"):
        with hold(common_dir, wait_s=0, holder_label="inner"):
            with hold(common_dir, wait_s=0, holder_label="innermost"):
                pass
        probe = _spawn(_TRY_ONCE_CHILD, str(common_dir))
        out, err = probe.communicate(timeout=60)
        assert out.strip() == "busy", "inner exit must not release the outer hold"
    assert object_store_lock._depth.__dict__.get("depths") == {}


def test_exception_releases_and_resets_depth(common_dir: Path):
    with pytest.raises(RuntimeError):
        with hold(common_dir, wait_s=0, holder_label="boom"):
            raise RuntimeError("boom")
    assert object_store_lock._depth.__dict__.get("depths") == {}
    with hold(common_dir, wait_s=0, holder_label="again"):
        pass


def test_busy_acquire_leaves_no_depth_behind(common_dir: Path, tmp_path: Path):
    ready = tmp_path / "ready"
    holder = _spawn(_HOLDER_CHILD, str(common_dir), str(ready), stdin=subprocess.PIPE)
    try:
        _wait_for(ready)
        with pytest.raises(ObjectStoreBusy):
            with hold(common_dir, wait_s=0, holder_label="contender"):
                pass
        assert object_store_lock._depth.__dict__.get("depths") == {}
    finally:
        holder.stdin.close()
        holder.communicate(timeout=60)


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
