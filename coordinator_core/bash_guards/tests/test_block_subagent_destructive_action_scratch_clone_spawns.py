"""A subagent may `git clone` anywhere (git refuses an occupied destination itself); hook, gitdir and program-running options stay refused."""

from __future__ import annotations

import subprocess

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.bash_guards import block_subagent_destructive_action as guard


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "scratch").mkdir()
    (root / "sub" / "deep").mkdir(parents=True)
    (tmp_path / "other" / ".git").mkdir(parents=True)
    return root


def _run(command, cwd):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess-clone",
        "agent_id": "deadbeef0123",
        "agent_type": "coordinator:executor",
    }
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return guard.check(payload)


def _p(path):
    return str(path).replace("\\", "/")


CLONE = "git clone --shared -n . scratch/c0-base/c"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", cwd=src)
    (src / "a.txt").write_text("a")
    _git("add", "a.txt", cwd=src)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=src)
    return src


def test_real_clone_into_empty_dir_outside_scratch_passes_and_runs(repo, source, tmp_path):
    dest = tmp_path / "out" / "empty"
    dest.mkdir(parents=True)
    cmd = f"git clone {_p(source)} {_p(dest)}"
    assert _run(cmd, repo) is None
    subprocess.run(cmd, shell=True, check=True, capture_output=True)
    assert (dest / "a.txt").read_text() == "a"


def test_real_clone_into_absent_dir_outside_scratch_passes_and_runs(repo, source, tmp_path):
    dest = tmp_path / "out" / "absent"
    cmd = f"git clone {_p(source)} {_p(dest)}"
    assert _run(cmd, repo) is None
    subprocess.run(cmd, shell=True, check=True, capture_output=True)
    assert (dest / "a.txt").exists()


def test_relative_absent_dest_outside_scratch_passes(repo, source):
    assert _run(f"git clone {_p(source)} fresh", repo) is None


@pytest.mark.parametrize("opt", ["--template=/x", "--separate-git-dir=/x/g", "-u /x", "--upload-pack=/x", "-c core.hooksPath=/x"])
def test_refused_options_stay_refused_for_an_empty_dest(repo, source, tmp_path, opt):
    dest = tmp_path / "out" / "empty2"
    dest.mkdir(parents=True)
    assert _run(f"git clone {opt} {_p(source)} {_p(dest)}", repo) is not None


@pytest.mark.parametrize("spelling", ["F:/dev/example-game-repo/clones/x", "F:\\dev\\example-game-repo\\clones\\x", "'F:\\dev\\x'"])
def test_drive_letter_dest_is_absolute_not_joined_onto_cwd(repo, source, spelling):
    assert _run(f"git clone {_p(source)} {spelling}", repo) is None
    assert "F:" not in {entry.name for entry in repo.iterdir()}


URL = "https://example.invalid/x/y.git"
