"""dispatch.terminal_commit regenerates the landing allowlist for a new published-tree name.

A run adding a top-level name under `coordinator/bin/` or `coordinator_core/`
cannot satisfy commit_paths' published-tree check on its own; the terminal
commit regenerates `setup/publish-targets.portable` for those paths and carries
it in the same commit.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO_ROOT = Path(__file__).resolve().parents[4]
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
_ENGINE_ROW = "claude-klabauter"
_BIN_ROW = "claude-klabauter-coordinator-bin"
_PORTABLE = "setup/publish-targets.portable"
_ENGINE_DIRS = ("hooks", "ops", "write_guards", "bash_guards", "frontmatter", "session", "contract")
_ENGINE_FILES = (
    "coordinator_core/frontmatter/schema_validate.py",
    "coordinator_core/contract/cockpit_schema/emit_schema.py",
)
_BIN_NAMES = ("publish-allowlist-generate.py", "old.py")


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _portable(bin_names) -> str:
    engine = ",".join(sorted(_ENGINE_DIRS))
    return (
        f"{_ENGINE_ROW}|mirror|sigil|coordinator_core|coordinator_core||{engine}\n"
        f"{_BIN_ROW}|mirror|sigil|coordinator/bin|coordinator/bin||{','.join(sorted(bin_names))}\n"
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "setup").mkdir(parents=True)
    (root / "coordinator" / "bin").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    shutil.copy(
        _REPO_ROOT / "coordinator" / "bin" / "publish-allowlist-generate.py",
        root / "coordinator" / "bin" / "publish-allowlist-generate.py",
    )
    (root / "coordinator" / "bin" / "old.py").write_text("x = 1\n", encoding="utf-8")
    for rel in _ENGINE_FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x = 1\n", encoding="utf-8")
    for name in _ENGINE_DIRS:
        (root / "coordinator_core" / name).mkdir(parents=True, exist_ok=True)
        (root / "coordinator_core" / name / "m.py").write_text("x = 1\n", encoding="utf-8")
    (root / "setup" / "publish-allowlist-declarations.yaml").write_text(
        f"rows:\n  {_ENGINE_ROW}:\n    deny: []\n  {_BIN_ROW}:\n    deny: []\n", encoding="utf-8"
    )
    (root / _PORTABLE).write_text(_portable(_BIN_NAMES), encoding="utf-8", newline="\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _fire(repo: Path, path: str) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(path,)),))
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": []},
        repo_root=repo / ".git",
    )


def test_a_new_bin_cli_lands_with_the_regenerated_allowlist(repo):
    (repo / "coordinator" / "bin" / "newtool.py").write_text("x = 1\n", encoding="utf-8")

    out = _fire(repo, "coordinator/bin/newtool.py")

    assert out["committed"] is True, out
    landed = _git(["show", "--name-only", "--format=", out["sha"]], repo).split()
    assert sorted(landed) == sorted([_PORTABLE, "coordinator/bin/newtool.py"])
    assert "newtool.py" in _git(["show", f"{out['sha']}:{_PORTABLE}"], repo)
    assert _git(["status", "--porcelain", "--", _PORTABLE], repo) == ""


def test_a_run_touching_neither_published_dir_leaves_the_allowlist_alone(repo):
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    before = (repo / _PORTABLE).read_bytes()

    out = _fire(repo, "a.py")

    assert out["committed"] is True, out
    assert _git(["show", "--name-only", "--format=", out["sha"]], repo).split() == ["a.py"]
    assert (repo / _PORTABLE).read_bytes() == before


def test_a_denied_name_is_not_admitted_and_the_commit_still_lands(repo):
    (repo / "setup" / "publish-allowlist-declarations.yaml").write_text(
        f"rows:\n  {_ENGINE_ROW}:\n    deny: []\n  {_BIN_ROW}:\n    deny: [secret.py]\n",
        encoding="utf-8",
    )
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "deny"], repo)
    (repo / "coordinator" / "bin" / "secret.py").write_text("x = 1\n", encoding="utf-8")

    out = _fire(repo, "coordinator/bin/secret.py")

    assert out["committed"] is True, out
    assert "secret.py" not in (repo / _PORTABLE).read_text(encoding="utf-8")


def test_a_generator_failure_leaves_the_commit_refused(repo):
    (repo / "coordinator" / "bin" / "publish-allowlist-generate.py").write_text(
        "raise RuntimeError('broken')\n", encoding="utf-8"
    )
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "break generator"], repo)
    (repo / "coordinator" / "bin" / "newtool.py").write_text("x = 1\n", encoding="utf-8")

    out = _fire(repo, "coordinator/bin/newtool.py")

    assert out["committed"] is False
    assert "landing allowlist does not classify" in out["error"]
