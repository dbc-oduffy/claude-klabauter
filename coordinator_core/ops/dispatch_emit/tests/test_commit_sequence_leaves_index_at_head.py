"""A terminal_commit landing followed by a commit_v2 of other paths leaves the index at HEAD.

Reproduces the example-retrieval-repo-d1 sequence: many landed paths (new files in new directories and
edits), then a commit_v2 of drift paths; `git diff --cached HEAD` must hold only what the user
deliberately pre-staged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = reviewed()
_NEW = [f"pkg{i % 4}/sub{i % 3}/new{i}.py" for i in range(30)]
_EDITED = [f"keep/edit{i}.py" for i in range(12)]


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
    (root / "keep").mkdir()
    for rel in _EDITED:
        (root / rel).write_text("head\n", encoding="utf-8")
    (root / "drift.py").write_text("head\n", encoding="utf-8")
    (root / "user.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def test_landing_then_commit_v2_leaves_index_at_head(repo):
    (repo / "user.py").write_text("a\nB-staged\nc\nd\n", encoding="utf-8")
    _git(["add", "user.py"], repo)
    for rel in _NEW:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("new\n", encoding="utf-8")
    for rel in _EDITED:
        (repo / rel).write_text("edited\n", encoding="utf-8")
    (repo / "drift.py").write_text("drifted\n", encoding="utf-8")

    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=tuple(_NEW + _EDITED)),)
    )
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    out = terminal_commit._handler(
        {"inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": []},
        repo_root=repo / ".git",
    )
    assert out["committed"] is True, out
    landed = _git(["rev-parse", "HEAD"], repo)

    second = commit_v2._handler(
        {"paths": ["drift.py"], "message": "drift"}, repo_root=repo / ".git"
    )
    assert second["committed"] is True, second
    assert _git(["rev-parse", "HEAD"], repo) != landed

    assert _git(["diff", "--cached", "--name-only", "HEAD"], repo).split() == ["user.py"]
    assert "B-staged" in _git(["show", ":user.py"], repo)
    assert "B-staged" not in _git(["show", "HEAD:user.py"], repo)


def _withhold(repo: Path, run: str, rels: list) -> None:
    import json

    d = repo / "state" / "warp" / run
    d.mkdir(parents=True, exist_ok=True)
    with (d / "withhold.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        for rel in rels:
            fh.write(json.dumps({"path": rel, "reason": "drift", "at": "t"}) + "\n")


def test_withheld_path_then_commit_v2_leaves_index_at_head(repo):
    run = "ask-20261016T110000-bbbbbb"
    for rel in ("keep/edit0.py", "keep/edit1.py"):
        (repo / rel).write_text("edited\n", encoding="utf-8")
    (repo / "new.py").write_text("new\n", encoding="utf-8")
    _withhold(repo, run, ["keep/edit1.py"])
    request = CommitRequest(chunks=(
        ChunkCommit(id="C1", title="t1", paths=("keep/edit0.py", "new.py")),
        ChunkCommit(id="C2", title="t2", paths=("keep/edit1.py",)),
    ))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, "run_id": run},
        repo_root=repo / ".git",
    )
    assert out["committed"] is True, out
    assert "keep/edit1.py" in out["run_artifacts"]["withheld"]
    second = commit_v2._handler(
        {"paths": ["keep/edit1.py"], "message": "withheld"}, repo_root=repo / ".git"
    )
    assert second["committed"] is True, second
    assert _git(["diff", "--cached", "--name-only", "HEAD"], repo) == ""


def test_splice_failure_after_the_ref_moves_is_repaired_by_reset(repo, monkeypatch):
    from coordinator_core.git import index_write

    (repo / "user.py").write_text("a\nB-staged\nc\nd\n", encoding="utf-8")
    _git(["add", "user.py"], repo)
    for rel in _EDITED[:3]:
        (repo / rel).write_text("edited\n", encoding="utf-8")
    (repo / "fresh.py").write_text("fresh\n", encoding="utf-8")

    def boom(self):
        self.abort()
        raise index_write.IndexWriteError("simulated splice failure")

    monkeypatch.setattr(index_write.PreparedSplice, "publish", boom)
    out = commit_v2._handler(
        {"paths": [*_EDITED[:3], "fresh.py"], "message": "land"}, repo_root=repo / ".git"
    )
    assert out["committed"] is True, out
    assert out.get("index_stale") in (None, [])
    assert _git(["diff", "--cached", "--name-only", "HEAD"], repo).split() == ["user.py"]
    assert "B-staged" in _git(["show", ":user.py"], repo)


def test_unrepairable_splice_failure_names_the_reset_command(repo, monkeypatch):
    from coordinator_core.git import commit, index_write

    (repo / "fresh.py").write_text("fresh\n", encoding="utf-8")

    def boom(self):
        self.abort()
        raise index_write.IndexWriteError("simulated splice failure")

    monkeypatch.setattr(index_write.PreparedSplice, "publish", boom)
    monkeypatch.setattr(commit, "_reset_index_paths", lambda *_a: False)
    out = commit_v2._handler({"paths": ["fresh.py"], "message": "land"}, repo_root=repo / ".git")
    assert out["committed"] is True, out
    assert out["index_stale"] == ["fresh.py"]
    assert "git reset -q --" in " ".join(out["warnings"])
