"""Index-only landing convergence is fleet-wide: `commit_paths` converges an untouched supplied
path and leaves an edited one alone in any target repo, including one with no claude-klabauter markers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.commit import commit_paths

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_MSG = "m\n\nSession-Id: 22222222-2222-4222-8222-222222222222"


def _run(repo: Path, args: list[str], data: bytes | None = None) -> str:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent))
    out = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, input=data, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    return out.decode().strip()


def _make_repo(root: Path, name: str, marker: bool) -> Path:
    repo = root / name
    repo.mkdir()
    _run(repo, ["init", "-q"])
    _run(repo, ["config", "user.email", "t@t.example"])
    _run(repo, ["config", "user.name", "t"])
    _run(repo, ["config", "core.autocrlf", "false"])
    files = {"a.txt": b"a0\n", "b.txt": b"b0\n"}
    if marker:
        (repo / ".coordinator-local").mkdir()
        (repo / ".coordinator-local" / "marker").write_bytes(b"claude-klabauter\n")
        files[".coordinator-local/marker"] = b"claude-klabauter\n"
    for rel, data in files.items():
        (repo / rel).write_bytes(data)
    _run(repo, ["add", "--", *files])
    _run(repo, ["commit", "-q", "-m", "seed"])
    return repo


@pytest.mark.parametrize("marker", [True, False], ids=["claude-klabauter-marked", "unmarked-second-repo"])
def test_landing_converges_untouched_and_spares_edited_in_each_target_repo(
    tmp_path, monkeypatch, marker
):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = _make_repo(tmp_path, "target", marker)
    new_a = _run(repo, ["hash-object", "-w", "--stdin"], b"a1\n")
    new_b = _run(repo, ["hash-object", "-w", "--stdin"], b"b1\n")
    (repo / "b.txt").write_bytes(b"b0\nedited meanwhile\n")

    outcome = commit_paths(
        repo, ["a.txt", "b.txt"], _MSG, supplied_blobs={"a.txt": new_a, "b.txt": new_b}
    )

    assert outcome.converged == ("a.txt",)
    assert outcome.left_alone == ("b.txt",)
    assert _run(repo, ["rev-parse", ":a.txt"]) == new_a
    assert _run(repo, ["hash-object", "a.txt"]) == new_a
    assert _run(repo, ["rev-parse", "HEAD:a.txt"]) == new_a
    assert _run(repo, ["status", "--porcelain", "--", "a.txt"]) == ""
    assert (repo / "b.txt").read_bytes() == b"b0\nedited meanwhile\n"
    assert _run(repo, ["rev-parse", "HEAD:b.txt"]) == new_b
