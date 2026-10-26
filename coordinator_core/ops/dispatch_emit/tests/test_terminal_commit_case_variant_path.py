"""dispatch.terminal_commit lands git's spelling of a case-variant declared write.

A chunk declaring `coordinator/bin/BLOCKED.py` for the tracked `blocked.py` must commit the
tracked file, never a `BLOCKED.py` entry, and a declared path nothing backs is dropped.
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
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
_REVIEWED = reviewed()
_ENGINE_ROW = "claude-klabauter"
_BIN_ROW = "claude-klabauter-coordinator-bin"
_PORTABLE = "setup/publish-targets.portable"
_ENGINE_DIRS = ("hooks", "ops", "write_guards", "bash_guards", "frontmatter", "session", "contract")
_ENGINE_FILES = (
    "coordinator_core/frontmatter/schema_validate.py",
    "coordinator_core/contract/cockpit_schema/emit_schema.py",
)
_BIN_NAMES = ("publish-allowlist-generate.py", "blocked.py")
_VARIANT = "coordinator/bin/BLOCKED.py"
_TRACKED = "coordinator/bin/blocked.py"
_GHOST_TEST = "coordinator/bin/tests/test_BLOCKED.py"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


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
    (root / _TRACKED).write_text("x = 1\n", encoding="utf-8")
    for rel in _ENGINE_FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x = 1\n", encoding="utf-8")
    for name in _ENGINE_DIRS:
        (root / "coordinator_core" / name).mkdir(parents=True, exist_ok=True)
        (root / "coordinator_core" / name / "m.py").write_text("x = 1\n", encoding="utf-8")
    (root / "setup" / "publish-allowlist-declarations.yaml").write_text(
        f"rows:\n  {_ENGINE_ROW}:\n    deny: []\n  {_BIN_ROW}:\n    deny: []\n", encoding="utf-8"
    )
    engine = ",".join(sorted(_ENGINE_DIRS))
    (root / _PORTABLE).write_text(
        f"{_ENGINE_ROW}|mirror|sigil|coordinator_core|coordinator_core||{engine}\n"
        f"{_BIN_ROW}|mirror|sigil|coordinator/bin|coordinator/bin||{','.join(sorted(_BIN_NAMES))}\n",
        encoding="utf-8", newline="\n",
    )
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _fire(repo: Path, paths) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=tuple(paths)),))
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": []},
        repo_root=repo / ".git",
    )


def test_a_case_variant_of_a_tracked_path_lands_the_tracked_spelling(repo):
    (repo / _TRACKED).write_text("x = 2\n", encoding="utf-8")

    out = _fire(repo, [_VARIANT, _GHOST_TEST])

    assert out["committed"] is True, out
    assert "error" not in out
    entries = _git(["ls-tree", "-r", "--name-only", out["sha"]], repo).split()
    assert _TRACKED in entries
    assert _VARIANT not in entries
    assert _GHOST_TEST not in entries
    landed = _git(["show", "--name-only", "--format=", out["sha"]], repo).split()
    assert _TRACKED in landed
    assert _VARIANT not in landed
    assert out["case_canonicalized"] == {_VARIANT: _TRACKED}


def test_an_exact_spelling_is_left_alone_and_reports_no_mapping(repo):
    (repo / _TRACKED).write_text("x = 2\n", encoding="utf-8")

    out = _fire(repo, [_TRACKED])

    assert out["committed"] is True, out
    assert "case_canonicalized" not in out


def test_a_new_file_in_an_existing_dir_lands_beside_a_canonicalized_variant(repo):
    (repo / _TRACKED).write_text("x = 2\n", encoding="utf-8")
    new = "coordinator/bin/fresh.py"
    (repo / new).write_text("y = 1\n", encoding="utf-8")

    out = _fire(repo, [_VARIANT, new])

    assert out["committed"] is True, out
    landed = _git(["show", "--name-only", "--format=", out["sha"]], repo).split()
    assert _TRACKED in landed and new in landed
    assert _VARIANT not in landed
