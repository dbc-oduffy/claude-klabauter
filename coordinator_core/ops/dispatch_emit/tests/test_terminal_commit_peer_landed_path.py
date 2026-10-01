"""dispatch.terminal_commit lands the remaining declared paths when a peer
already committed or staged one of them."""

from __future__ import annotations

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


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


# A marked run lands only with review-stage output; tests that aren't about it carry this one.
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


def _call(repo: Path, params: dict) -> dict:
    return terminal_commit._handler({"inline_review": _REVIEWED, **params}, repo_root=repo / ".git")


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
    return root


def _write_script(repo: Path, request: CommitRequest, name: str = "run.mjs") -> str:
    marker = render_marker(request)
    text = "// emitted script\n"
    if marker is not None:
        text += marker + "\n"
    (repo / name).write_text(text, encoding="utf-8")
    return name


def test_peer_committed_path_is_reported_and_the_rest_lands(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    _git(["add", "a.py"], repo)
    _git(["commit", "-q", "-m", "peer"], repo)
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t1", paths=("a.py",)),
            ChunkCommit(id="C2", title="t2", paths=("b.py",)),
        ),
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is True, out
    assert out["no_delta"] == ["a.py"]
    assert any(
        "a.py" in w and "already at HEAD" in w for w in out.get("warnings", [])
    ), out.get("warnings")
    numstat = _git(["show", "--numstat", "--format=", "HEAD"], repo)
    changed = [ln.split("\t")[-1] for ln in numstat.splitlines() if ln.strip()]
    assert changed == ["b.py"]


def test_peer_staged_path_is_overwritten_by_the_worktree(repo):
    a = repo / "a.py"
    a.write_text("peer\n", encoding="utf-8")
    _git(["add", "a.py"], repo)
    a.write_text("ours\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),),
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is True, out
    assert _git(["show", "HEAD:a.py"], repo) == "ours\n"
    assert "a.py" in out["worktree_over_staged"]
