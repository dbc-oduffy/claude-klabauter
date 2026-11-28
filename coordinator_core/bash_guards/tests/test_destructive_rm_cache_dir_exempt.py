"""check_destructive_rm passes regenerable cache dirs inside a repo and keeps
guarding look-alike names and the same dirs outside any repo."""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.bash_guards.dispatch_checks import check_destructive_rm
from coordinator_core.guard_messages.testing import envelope_of
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(*args: str, cwd: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
        **no_console_creationflags(),
    )


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", cwd=str(root))
    _git("config", "user.email", "t@t.invalid", cwd=str(root))
    _git("config", "user.name", "t", cwd=str(root))
    (root / "tracked.txt").write_text("committed\n")
    _git("add", "tracked.txt", cwd=str(root))
    _git("commit", "-qm", "init", cwd=str(root))
    return root


def _dirty_dir(path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "untracked.txt").write_text("uncommitted\n")


def _check(cmd: str):
    return envelope_of(check_destructive_rm(cmd))


@pytest.mark.parametrize("name", ["__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"])
def test_cache_dir_in_repo_passes(repo, name):
    _dirty_dir(repo / "pkg" / name)
    assert _check(f"rm -rf {repo / 'pkg' / name}") is None


def test_cache_dir_at_repo_root_passes(repo):
    _dirty_dir(repo / "__pycache__")
    assert _check(f"rm -rf {repo / '__pycache__'}") is None


@pytest.mark.parametrize("name", ["__pycache__x", "my__pycache__", "pycache", ".pytest_cache2", "cache"])
def test_lookalike_name_stays_denied(repo, name):
    _dirty_dir(repo / "pkg" / name)
    assert _check(f"rm -rf {repo / 'pkg' / name}") is not None


def test_dotdot_escape_to_non_cache_stays_denied(repo):
    _dirty_dir(repo / "pkg" / "Saved")
    _dirty_dir(repo / "pkg" / "__pycache__")
    assert _check(f"rm -rf {repo / 'pkg' / '__pycache__' / '..' / 'Saved'}") is not None


def test_cache_dir_outside_any_repo_not_exempted_by_this_leg(tmp_path):
    from coordinator_core.bash_guards._dc_rm import _is_repo_regenerable_cache_dir

    _dirty_dir(tmp_path / "norepo" / "__pycache__")
    assert _is_repo_regenerable_cache_dir(str(tmp_path / "norepo" / "__pycache__")) is False
