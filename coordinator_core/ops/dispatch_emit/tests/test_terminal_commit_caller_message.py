"""A caller `message` param rides the terminal commit body ahead of the engine trailers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {
    "integration_stem": "rev-stem", "slices": 3, "fixes": 3,
    "wave_reviews": reviewed()["wave_reviews"],
}
_CALLER = "terminal commit: own-paths XS budget -- Closes rows b59ee4, 6c1d83"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "d.py").write_text("d\n", encoding="utf-8")
    (root / "report-C1.md").write_text("DONE: ok\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="land it", paths=("d.py",), report="report-C1.md"),))
    (root / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return root


def _land(repo: Path, **extra) -> str:
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, **extra}, repo_root=repo / ".git",
    )
    assert out["committed"] is True, out
    return _git(["log", "-1", "--format=%B"], repo)


def test_caller_message_precedes_engine_trailers(repo):
    body = _land(repo, message=_CALLER)
    assert body.startswith("C1: land it")
    assert _CALLER in body
    assert body.index(_CALLER) < body.index("Inline-Review:")


def test_absent_or_blank_message_adds_nothing(repo):
    body = _land(repo, message="   ")
    assert "terminal commit:" not in body
    assert body.startswith("C1: land it")
