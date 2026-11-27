"""A subagent rm -r/-f whose every target resolves under <repo>/scratch/ is allowed; escapes stay denied."""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from pathlib import Path

RM = "r" + "m"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
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


def _denied(result) -> bool:
    return bool(result) and "rm -r/-f" in str(result)


def test_scratch_target_allowed_per_command_and_per_segment(repo) -> None:
    assert not _denied(_run(f"{RM} -rf scratch/x", repo))
    assert not _denied(_run(f"echo ok && {RM} -rf scratch/x", repo))
    assert not _denied(_run(f"{RM} -rf scratch/x scratch/y", repo))


def test_absolute_scratch_target_allowed(repo) -> None:
    assert not _denied(_run(f"{RM} -rf {repo.as_posix()}/scratch/x", repo / "src"))


def test_glob_in_last_component_under_scratch_allowed(repo) -> None:
    assert not _denied(_run(f"{RM} -rf scratch/*", repo))


def test_dotdot_escape_denied_as_before(repo) -> None:
    assert _denied(_run(f"{RM} -rf scratch/../src", repo))
    assert _denied(_run(f"{RM} -rf scratch/x/../../src", repo))


def test_scratch_root_itself_and_non_scratch_denied(repo) -> None:
    assert _denied(_run(f"{RM} -rf scratch", repo))
    assert _denied(_run(f"{RM} -rf src", repo))
    assert _denied(_run(f"{RM} -rf scratch/x src", repo))


def test_cd_before_rm_denied(repo) -> None:
    assert _denied(_run(f"cd scratch && {RM} -rf x", repo))


def test_unresolvable_operands_denied(repo) -> None:
    assert _denied(_run(f"{RM} -rf scratch/$X", repo))
    assert _denied(_run(f"{RM} -rf scratch/*/x", repo))


def test_hidden_rm_in_another_segment_denied(repo) -> None:
    assert _denied(_run(f"echo scratch/x | xargs {RM} -rf", repo))


def test_link_under_scratch_pointing_outside_denied(repo) -> None:
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


@pytest.fixture
def machine_scratch(tmp_path: Path, monkeypatch) -> Path:
    from coordinator_core import machine_resolver

    root = tmp_path / "machine-scratch"
    clone = root / "proj" / "mirror-clone"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        machine_resolver,
        "registry_get",
        lambda key: str(root) if key == "paths.scratch_root" else None,
    )
    return root


def test_clone_root_under_machine_scratch_root_allowed(repo, machine_scratch) -> None:
    clone = (machine_scratch / "proj" / "mirror-clone").as_posix()
    assert not _denied(_run(f"{RM} -rf {clone}", repo))
    assert not _denied(_run(f"{RM} -rf {clone} {clone}/.git", repo / "src"))


def test_clone_root_outside_machine_scratch_root_denied(repo, machine_scratch, tmp_path) -> None:
    other = tmp_path / "elsewhere" / "mirror-clone"
    (other / ".git").mkdir(parents=True)
    assert _denied(_run(f"{RM} -rf {other.as_posix()}", repo))
    assert _denied(_run(f"{RM} -rf {(machine_scratch / 'proj' / 'mirror-clone').as_posix()} {other.as_posix()}", repo))


def test_machine_scratch_root_itself_and_dotdot_escape_denied(repo, machine_scratch, tmp_path) -> None:
    assert _denied(_run(f"{RM} -rf {machine_scratch.as_posix()}", repo))
    assert _denied(_run(f"{RM} -rf {machine_scratch.as_posix()}/proj/../../repo", repo))


def test_unset_machine_scratch_root_denies_clone(repo, tmp_path, monkeypatch) -> None:
    from coordinator_core import machine_resolver

    clone = tmp_path / "machine-scratch" / "proj" / "mirror-clone"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: None)
    assert _denied(_run(f"{RM} -rf {clone.as_posix()}", repo))


def _run_no_cwd(command):
    return guard.check(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "session_id": "s",
            "agent_id": "deadbeef0123",
            "agent_type": "coordinator:test-runner",
        }
    )


def test_no_cwd_relative_operand_still_denied(repo) -> None:
    assert _denied(_run_no_cwd(f"{RM} -rf scratch/x"))


def test_no_cwd_absolute_machine_scratch_operand_allowed(machine_scratch) -> None:
    target = machine_scratch / "proj" / "x"
    target.mkdir(parents=True)
    assert not _denied(_run_no_cwd(f"{RM} -rf {target.as_posix()}"))
    assert not _denied(_run_no_cwd(f"{RM} -rf {target.as_posix()}/*"))


def test_cwd_at_repo_root_absolute_machine_scratch_operand_allowed(repo, machine_scratch) -> None:
    target = machine_scratch / "proj" / "pytest-1"
    target.mkdir(parents=True)
    assert not _denied(_run(f"{RM} -rf {target.as_posix()}", repo))


def test_cwd_at_repo_root_machine_scratch_root_itself_and_escape_denied(repo, machine_scratch) -> None:
    (machine_scratch / "proj").mkdir(exist_ok=True)
    assert _denied(_run(f"{RM} -rf {machine_scratch.as_posix()}", repo))
    assert _denied(_run(f"{RM} -rf {machine_scratch.as_posix()}/proj/../../outside", repo))


def test_no_cwd_scratch_root_itself_denied(machine_scratch) -> None:
    assert _denied(_run_no_cwd(f"{RM} -rf {machine_scratch.as_posix()}"))


def test_no_cwd_dotdot_escape_denied(machine_scratch, tmp_path) -> None:
    (machine_scratch / "proj").mkdir(exist_ok=True)
    assert _denied(_run_no_cwd(f"{RM} -rf {machine_scratch.as_posix()}/proj/../../outside"))


def test_no_cwd_outside_scratch_and_unset_root_denied(machine_scratch, tmp_path, monkeypatch) -> None:
    outside = tmp_path / "elsewhere" / "x"
    outside.mkdir(parents=True)
    assert _denied(_run_no_cwd(f"{RM} -rf {outside.as_posix()}"))
    inside = machine_scratch / "proj" / "x"
    inside.mkdir(parents=True)
    from coordinator_core import machine_resolver

    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: None)
    assert _denied(_run_no_cwd(f"{RM} -rf {inside.as_posix()}"))


def test_no_cwd_variable_and_tilde_operands_denied(machine_scratch) -> None:
    assert _denied(_run_no_cwd(f"{RM} -rf {machine_scratch.as_posix()}/$X"))
    assert _denied(_run_no_cwd(f"{RM} -rf ~/{machine_scratch.name}/x"))


def test_no_cwd_junction_out_of_scratch_denied(machine_scratch, tmp_path) -> None:
    outside = tmp_path / "outside"
    (outside / "victim").mkdir(parents=True)
    link = machine_scratch / "lnk"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            raise
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode != 0:
            pytest.skip("cannot create a directory link here")
    assert _denied(_run_no_cwd(f"{RM} -rf {link.as_posix()}/victim"))
    assert _denied(_run_no_cwd(f"{RM} -rf {link.as_posix()}"))
