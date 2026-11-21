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

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete, pytest.mark.cadence]

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


def _driver_clone(proc: subprocess.CompletedProcess) -> Path:
    assert proc.returncode == 0, proc.stderr
    out = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    return Path(out["DRIVER"]).resolve().parents[2]


def test_runs_a_commit_clone_when_cwd_is_inside_its_own_tree(tmp_path):
    repo = _real_repo(tmp_path / "repo")
    clone = _driver_clone(_run_dirty(repo, repo, tmp_path))
    assert clone.name == _git(repo, "rev-parse", "HEAD")


def test_runs_a_commit_clone_when_cwd_is_in_no_checkout(tmp_path):
    repo = _real_repo(tmp_path / "repo")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    clone = _driver_clone(_run_dirty(repo, elsewhere, tmp_path))
    assert clone.name == _git(repo, "rev-parse", "HEAD")


def test_failed_materialise_refuses_and_never_runs_the_live_tree(tmp_path):
    repo = _real_repo(tmp_path / "repo")
    live_driver = repo / "coordinator" / "bin" / "publish.py"
    live_driver.write_text("print('LIVE-DRIVER-RAN')\n", encoding="utf-8")
    (repo / "coordinator" / "bin" / "lib" / "commit_clone.py").write_text(
        textwrap.dedent(
            """
            from pathlib import Path
            def is_commit_clone(root): return False
            def default_cache_root(): return Path("unused")
            def materialise_commit_clone(*a, **k): raise RuntimeError("injected materialise failure")
            """
        ),
        encoding="utf-8",
    )
    proc = _run_dirty(repo, repo, tmp_path)
    assert proc.returncode == 1
    assert "refused" in proc.stderr and "nothing ran from the live tree" in proc.stderr
    assert "Retry:" in proc.stderr
    assert "LIVE-DRIVER-RAN" not in proc.stdout and "DRIVER=" not in proc.stdout


def test_unresolvable_commit_refuses_and_never_runs_the_live_tree(tmp_path):
    repo = _real_repo(tmp_path / "repo")
    proc = _run_dirty(repo, repo, tmp_path, "--commit", "no-such-ref")
    assert proc.returncode == 1
    assert "refused" in proc.stderr and "Retry:" in proc.stderr
    assert "DRIVER=" not in proc.stdout


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _real_repo(root: Path) -> Path:
    """A committed git repo carrying the real wrapper, commit_clone and temp_layout beside a stub driver."""
    bin_dir = root / "coordinator" / "bin"
    (bin_dir / "lib").mkdir(parents=True)
    (root / "coordinator_core").mkdir()
    (root / "coordinator_core" / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(_WRAPPER.parents[2] / "coordinator_core" / "temp_layout.py", root / "coordinator_core" / "temp_layout.py")
    shutil.copy2(_WRAPPER, bin_dir / "coordinator-publish.py")
    shutil.copy2(_WRAPPER.parent / "lib" / "commit_clone.py", bin_dir / "lib" / "commit_clone.py")
    (bin_dir / "publish.py").write_text(_STUB_DRIVER, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    # A non-dry-run publish pushes its source commit to origin before stamping; a tmp bare origin only.
    origin = root.parent / (root.name + "-origin.git")
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    _git(root, "remote", "add", "origin", str(origin))
    return root


def _run_dirty(repo: Path, cwd: Path, tmp_path: Path, *argv: str) -> subprocess.CompletedProcess:
    cache = tmp_path / "tmp"
    cache.mkdir(exist_ok=True)
    env = {**os.environ, "TMP": str(cache), "TEMP": str(cache), "TMPDIR": str(cache)}
    return subprocess.run(
        [sys.executable, str(repo / "coordinator" / "bin" / "coordinator-publish.py"), *argv],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120, **no_console_creationflags(),
    )


def test_dirty_live_tree_publishes_from_a_commit_clone_and_never_refuses(tmp_path):
    repo = _real_repo(tmp_path / "repo")
    sha = _git(repo, "rev-parse", "HEAD")
    live_driver = repo / "coordinator" / "bin" / "publish.py"
    live_driver.write_text("print('DIRTY-DRIVER-RAN')\nraise SystemExit(9)\n", encoding="utf-8")
    proc = _run_dirty(repo, repo, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "publisher code differs" not in proc.stderr
    assert "DIRTY-DRIVER-RAN" not in proc.stdout
    out = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    driver = Path(out["DRIVER"]).resolve()
    assert driver.parents[2].name == sha
    assert driver.parents[3].name == "claude-klabauter-commit-clone"
    assert (driver.parents[2] / ".git").is_dir()
    assert _same(out["CORE"], driver.parents[2] / "coordinator_core" / "__init__.py")
    assert live_driver.read_text(encoding="utf-8").startswith("print('DIRTY-DRIVER-RAN')")


def test_explicit_commit_materialises_that_commit_not_head(tmp_path):
    repo = _real_repo(tmp_path / "repo")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "coordinator" / "bin" / "lib" / "later.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "later")
    proc = _run_dirty(repo, tmp_path, tmp_path, "--commit", first)
    assert proc.returncode == 0, proc.stderr
    out = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    clone = Path(out["DRIVER"]).resolve().parents[2]
    assert clone.name == first
    assert not (clone / "coordinator" / "bin" / "lib" / "later.py").exists()


def test_a_clone_is_not_cloned_again(tmp_path):
    repo = _real_repo(tmp_path / "repo")
    first = _run_dirty(repo, repo, tmp_path)
    clone_root = Path(dict(line.split("=", 1) for line in first.stdout.splitlines() if "=" in line)["DRIVER"]).resolve().parents[2]
    cache = clone_root.parent
    before = sorted(p.name for p in cache.iterdir())
    again = subprocess.run(
        [sys.executable, str(clone_root / "coordinator" / "bin" / "coordinator-publish.py")],
        cwd=str(tmp_path), env={**os.environ, "TMP": str(tmp_path / "tmp"), "TEMP": str(tmp_path / "tmp"), "TMPDIR": str(tmp_path / "tmp")},
        capture_output=True, text=True, timeout=120, **no_console_creationflags(),
    )
    assert again.returncode == 0, again.stderr
    assert sorted(p.name for p in cache.iterdir()) == before
    out = dict(line.split("=", 1) for line in again.stdout.splitlines() if "=" in line)
    assert _same(out["DRIVER"], clone_root / "coordinator" / "bin" / "publish.py")


def test_enclosing_checkout_without_publish_driver_is_not_a_clone(tmp_path):
    repo = _real_repo(tmp_path / "repo")
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    clone = _driver_clone(_run_dirty(repo, other, tmp_path))
    assert clone.name == _git(repo, "rev-parse", "HEAD")
