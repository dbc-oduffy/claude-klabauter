"""check_destructive_git_clean denies an executed git clean, never `git clean` text inside another command's quoted argument."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards._dc_git_misc import check_destructive_git_clean

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q"], cwd=str(tmp_path), check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    (tmp_path / "untracked-handoff.md").write_text("x")
    return tmp_path


def _check(cmd: str, repo: Path):
    return check_destructive_git_clean(cmd, payload={"cwd": str(repo)})


@pytest.mark.parametrize(
    "cmd",
    [
        'grep -n "irreversible\\|git clean\\|reset --hard" some_file.py | head -20',
        'cd . && R=1; grep -n "irreversible\\|git clean\\|reset --hard" some_file.py | head -20',
        'grep -n "git clean" some_file.py',
        'rg "git clean" .',
        'rg "foo|git clean -fd" .',
        'echo "git clean -fd"',
        'echo "a; git clean -fd"',
        "echo 'x && git clean -fdx'",
    ],
)
def test_quoted_mention_is_not_denied(repo: Path, cmd: str) -> None:
    assert _check(cmd, repo) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "git clean -fd",
        "echo hi && git clean -fd",
        "cd . ; git clean -fd",
        "(git clean -fd)",
        "$(git clean -fd)",
        "{ git clean -fd; }",
        "sudo git clean -fd",
        "git -C . clean -fd",
        "FOO=1 git clean -fd",
        'sh -c "git clean -fdx"',
        'eval "git clean -fdx"',
        'grep -n "git clean" some_file.py; git clean -fd',
    ],
)
def test_executed_git_clean_is_still_denied(repo: Path, cmd: str) -> None:
    result = _check(cmd, repo)
    assert result is not None
    assert "untracked-handoff.md" in str(result)
