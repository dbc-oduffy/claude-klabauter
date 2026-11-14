"""A peer's dirty setup/publish-targets.portable outside the pathspec is never
overwritten: the HEAD-based regenerated field 7 lands as an index-only blob.

Replaces the refusal-remedy pin, whose premise 56fa06679f retired."""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

_PORTABLE = (
    "claude-klabauter-coordinator-bin|mirror|publish-mirror:x|coordinator/bin|"
    "coordinator/bin||existing_cli.py,classified-tool.py\n"
)

_DECLARATIONS = "rows:\n  claude-klabauter-coordinator-bin:\n    deny: []\n"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    )


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/p")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    _write(r / "setup" / "publish-targets.portable", _PORTABLE)
    _write(r / "setup" / "publish-allowlist-declarations.yaml", _DECLARATIONS)
    _write(r / "coordinator" / "bin" / "existing_cli.py", "x = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


def test_peer_dirty_portable_is_regenerated_from_head_and_lands_index_only(repo):
    _write(repo / "coordinator" / "bin" / "new-tool.py", "x = 1\n")
    peer = _PORTABLE + "# peer edit\n"
    _write(repo / "setup" / "publish-targets.portable", peer)

    gcommit.commit_paths(repo, ["coordinator/bin/new-tool.py"], "bin tools")

    landed = _git(repo, "show", "HEAD:setup/publish-targets.portable").stdout
    assert "new-tool.py" in landed
    assert "# peer edit" not in landed
    assert (repo / "setup" / "publish-targets.portable").read_text(encoding="utf-8") == peer
