"""A newline separates commands for the destructive-rm guard exactly as `;` does.

Unsplit, `rm <file>` + newline + `cd <repo>` read `cd` and `<repo>` as further rm
targets and denied the repo root -- a command that deletes one scratch file.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture
def repos(tmp_path, monkeypatch):
    # The guard resolves its repo from the process cwd and records touch claims under it.
    here, other = tmp_path / "here", tmp_path / "other"
    for root in (here, other):
        subprocess.run(["git", "init", "-q", str(root)], check=True, **no_console_passthrough_kwargs())
    (here / "scratch.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.chdir(here)
    return here, other


def _verdict(cmd: str, cwd):
    return dispatch_checks.check_destructive_rm(
        cmd, "test-session", {"cwd": str(cwd), "tool_input": {"command": cmd}}
    )


@pytest.mark.parametrize("sep", ["\n", "; ", "\n\n"])
def test_rm_then_cd_to_another_repo_is_allowed(repos, sep):
    here, other = repos
    cmd = f"rm scratch.txt{sep}cd {other.as_posix()} && git log -1"
    assert _verdict(cmd, here) is None


def test_rm_of_a_repo_root_on_a_later_line_still_denies(repos):
    here, other = repos
    cmd = f"echo hi\nrm -rf {other.as_posix()}"
    verdict = _verdict(cmd, here)
    assert verdict and verdict["hookSpecificOutput"]["permissionDecision"] == "deny"
