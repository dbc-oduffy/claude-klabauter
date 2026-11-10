"""An inferred sibling test path in a chunk's ``paths`` is advisory: only the declared subset gates
``declared-write-missing``; the emitter infers no test path under a tests/ directory that is absent."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.pathspec import candidate_test_additions
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

_REVIEWED = {
    "integration_stem": "rev-stem", "slices": 1, "fixes": 1,
    "wave_reviews": reviewed()["wave_reviews"],
}
_SID = "11111111-2222-3333-4444-555555555555"
SRC = "tools/likeness-prototype/likeness/conform_preflight.py"
INFERRED = "tools/likeness-prototype/likeness/tests/test_conform_preflight.py"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tools/likeness-prototype/likeness").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _stage(root: Path, *, declared) -> str:
    (root / SRC).write_text("x = 1\n", encoding="utf-8")
    (root / "report-C1.md").write_text("DONE: built\n", encoding="utf-8")
    chunk = ChunkCommit(id="C1", title="t", paths=(SRC, INFERRED), report="report-C1.md", declared=declared)
    (root / "run.mjs").write_text("// emitted\n" + render_marker(CommitRequest(chunks=(chunk,))) + "\n", encoding="utf-8")
    return "run.mjs"


def _call(root: Path, script: str) -> dict:
    params = {"script_path": script, "incomplete_chunks": [], "inline_review": _REVIEWED, "session_id": _SID}
    return terminal_commit._handler(params, repo_root=root / ".git")


@pytest.mark.spawns_process
def test_absent_inferred_test_path_does_not_refuse(repo):
    out = _call(repo, _stage(repo, declared=(SRC,)))

    assert out.get("refused") != "declared-write-missing", out
    assert out["committed"] is True, out
    assert SRC in _git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split()


@pytest.mark.spawns_process
def test_absent_declared_path_still_refuses(repo):
    out = _call(repo, _stage(repo, declared=(SRC, INFERRED)))

    assert out["refused"] == "declared-write-missing", out
    assert out["missing_declared"] == {"C1": [INFERRED]}, out


@pytest.mark.spawns_process
def test_marker_without_declared_field_gates_on_every_path(repo):
    out = _call(repo, _stage(repo, declared=None))

    assert out["refused"] == "declared-write-missing", out


def test_emitter_skips_candidate_under_absent_tests_dir(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("", encoding="utf-8")

    assert candidate_test_additions(["pkg/mod.py"], tmp_path) == []

    (tmp_path / "pkg" / "tests").mkdir()
    present = candidate_test_additions(["pkg/mod.py"], tmp_path)
    assert len(present) == 1 and present[0].endswith("pkg/tests/test_mod.py")
