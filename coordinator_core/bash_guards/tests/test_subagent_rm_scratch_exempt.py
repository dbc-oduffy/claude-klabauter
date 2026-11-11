"""A subagent rm -r/-f whose every target resolves under <repo>/scratch/ is allowed; escapes stay denied."""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard

RM = "r" + "m"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "scratch" / "x").mkdir(parents=True)
    (root / "src").mkdir()
    return root


def _run(command, cwd):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess-rm-scratch",
        "agent_id": "deadbeef0123",
        "agent_type": "coordinator:executor",
        "cwd": str(cwd),
    }
    return guard.check(payload)


def _denied(result):
    return bool(result) and "rm -r/-f" in str(result)


def test_scratch_target_allowed_per_command_and_per_segment(repo):
    assert not _denied(_run(f"{RM} -rf scratch/x", repo))
    assert not _denied(_run(f"echo ok && {RM} -rf scratch/x", repo))
    assert not _denied(_run(f"{RM} -rf scratch/x scratch/y", repo))


def test_absolute_scratch_target_allowed(repo):
    assert not _denied(_run(f"{RM} -rf {repo.as_posix()}/scratch/x", repo / "src"))


def test_glob_in_last_component_under_scratch_allowed(repo):
    assert not _denied(_run(f"{RM} -rf scratch/*", repo))


def test_dotdot_escape_denied_as_before(repo):
    assert _denied(_run(f"{RM} -rf scratch/../src", repo))
    assert _denied(_run(f"{RM} -rf scratch/x/../../src", repo))


def test_scratch_root_itself_and_non_scratch_denied(repo):
    assert _denied(_run(f"{RM} -rf scratch", repo))
    assert _denied(_run(f"{RM} -rf src", repo))
    assert _denied(_run(f"{RM} -rf scratch/x src", repo))


def test_cd_before_rm_denied(repo):
    assert _denied(_run(f"cd scratch && {RM} -rf x", repo))


def test_unresolvable_operands_denied(repo):
    assert _denied(_run(f"{RM} -rf scratch/$X", repo))
    assert _denied(_run(f"{RM} -rf scratch/*/x", repo))


def test_hidden_rm_in_another_segment_denied(repo):
    assert _denied(_run(f"echo scratch/x | xargs {RM} -rf", repo))


def test_link_under_scratch_pointing_outside_denied(repo):
    link = repo / "scratch" / "lnk"
    try:
        os.symlink(repo / "src", link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            raise
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(repo / "src")],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode != 0:
            pytest.skip("cannot create a directory link here")
    assert _denied(_run(f"{RM} -rf scratch/lnk/x", repo))
    assert _denied(_run(f"{RM} -rf scratch/lnk/*", repo))


def test_no_cwd_leaves_deny_unchanged(repo):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"{RM} -rf scratch/x"},
        "session_id": "s",
        "agent_id": "deadbeef0123",
        "agent_type": "coordinator:executor",
    }
    assert _denied(guard.check(payload))
