"""coordinator-publish runs the invoking clone's publish.py and coordinator_core, not the
registered worktree's. Drives the real wrapper file at default arguments against tmp_path trees."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

_WRAPPER = Path(__file__).resolve().parents[1] / "coordinator-publish.py"

_STUB_DRIVER = textwrap.dedent(
    """
    import sys
    import coordinator_core
    print("DRIVER=" + __file__)
    print("CORE=" + coordinator_core.__file__)
    sys.exit(0)
    """
)


def _make_tree(root: Path, *, git: bool, wrapper: bool) -> Path:
    bin_dir = root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "publish.py").write_text(_STUB_DRIVER, encoding="utf-8")
    (root / "coordinator_core").mkdir()
    (root / "coordinator_core" / "__init__.py").write_text("", encoding="utf-8")
    if wrapper:
        shutil.copy2(_WRAPPER, bin_dir / "coordinator-publish.py")
    if git:
        (root / ".git").mkdir()
    return root


def _run(worktree: Path, cwd: Path, *, own_core_on_path: bool = False) -> dict[str, str]:
    env = None
    if own_core_on_path:
        # The own-tree legs put no root on sys.path; an interpreter without an installed
        # coordinator_core needs the worktree root handed to it.
        env = {**os.environ, "PYTHONPATH": str(worktree)}
    proc = subprocess.run(
        [sys.executable, str(worktree / "coordinator" / "bin" / "coordinator-publish.py")],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)


def _same(a: str, b: Path) -> bool:
    return Path(a).resolve() == b.resolve()


def test_runs_clone_driver_and_core_when_launched_from_a_clone(tmp_path):
    worktree = _make_tree(tmp_path / "worktree", git=True, wrapper=True)
    clone = _make_tree(tmp_path / "clone", git=True, wrapper=False)
    sub = clone / "scratch"
    sub.mkdir()
    out = _run(worktree, sub)
    assert _same(out["DRIVER"], clone / "coordinator" / "bin" / "publish.py")
    assert _same(out["CORE"], clone / "coordinator_core" / "__init__.py")


def test_in_process_leg_with_worktree_paths_already_on_sys_path(tmp_path):
    """The Windows leg: exec_cli has put the worktree root and bin ahead of everything."""
    worktree = _make_tree(tmp_path / "worktree", git=True, wrapper=True)
    clone = _make_tree(tmp_path / "clone", git=True, wrapper=False)
    wbin = worktree / "coordinator" / "bin"
    harness = tmp_path / "harness.py"
    harness.write_text(
        textwrap.dedent(
            f"""
            import runpy, sys
            sys.path[:0] = [{str(wbin)!r}, {str(worktree)!r}]
            sys.argv = [{str(wbin / 'coordinator-publish.py')!r}]
            runpy.run_path(sys.argv[0], run_name="__main__")
            """
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(harness)],
        cwd=str(clone),
        capture_output=True,
        text=True,
        timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    out = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    assert _same(out["DRIVER"], clone / "coordinator" / "bin" / "publish.py")
    assert _same(out["CORE"], clone / "coordinator_core" / "__init__.py")


def test_runs_own_driver_when_cwd_is_inside_its_own_tree(tmp_path):
    worktree = _make_tree(tmp_path / "worktree", git=True, wrapper=True)
    out = _run(worktree, worktree, own_core_on_path=True)
    assert _same(out["DRIVER"], worktree / "coordinator" / "bin" / "publish.py")


def test_runs_own_driver_when_cwd_is_in_no_checkout(tmp_path):
    worktree = _make_tree(tmp_path / "worktree", git=True, wrapper=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out = _run(worktree, elsewhere, own_core_on_path=True)
    assert _same(out["DRIVER"], worktree / "coordinator" / "bin" / "publish.py")


def test_enclosing_checkout_without_publish_driver_is_not_a_clone(tmp_path):
    worktree = _make_tree(tmp_path / "worktree", git=True, wrapper=True)
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    out = _run(worktree, other, own_core_on_path=True)
    assert _same(out["DRIVER"], worktree / "coordinator" / "bin" / "publish.py")
