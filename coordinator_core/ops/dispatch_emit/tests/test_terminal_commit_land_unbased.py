"""An unbased land (no resolvable run base, so the kept blob lands as-is) is loud: `unbased` on the reply,
`Land-Unbased:` on the landing commit and each receipt; a based land carries none of them."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000d4"
PATH = "pkg/execute_review.py"
RUN = "ask-20261027T120000-d4d4d4"
OWN_TEXT = "x = 2\n"
PEER_TEXT = "x = 2\npeer = 1\n"


def _git(args, cwd: Path, stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, check=True,
        input=None if stdin is None else stdin.encode(), **no_console_creationflags(),
    ).stdout.decode().strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "pkg").mkdir()
    (root / PATH).write_text("x = 1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    return root


def _land(repo: Path, monkeypatch, *, run_base: str | None) -> dict:
    blob = _git(["hash-object", "-w", "--stdin"], repo, stdin=OWN_TEXT)
    # The supply seam is stubbed: which paths are peer-held is the hold lookup's concern, not this file's.
    monkeypatch.setattr(terminal_commit, "_kept_blob_supply", lambda *a, **k: {PATH: blob})
    if run_base:
        marker = repo / "scratch" / "warp" / RUN / "run-base"
        marker.parent.mkdir(parents=True)
        marker.write_text(run_base + "\n", encoding="utf-8")
    (repo / PATH).write_text(PEER_TEXT, encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(PATH,)),))
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    sidecar = repo.parent / "review-sidecar.md"
    sidecar.write_text("---\nverdict: PASS\n---\n\nreview\n", encoding="utf-8")
    review = {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "wave_reviews": [{"slice_id": "s1", "verdict": "PASS", "sidecar_path": str(sidecar)}],
    }
    params = {
        "inline_review": review, "script_path": "run.mjs", "incomplete_chunks": [],
        "session_id": SELF, "run_id": RUN,
    }
    return terminal_commit._handler(params, repo_root=repo / ".git")


def test_an_unbased_land_names_the_path_on_the_reply_and_the_commit_trailer(repo, monkeypatch):
    out = _land(repo, monkeypatch, run_base=None)

    assert out["committed"] is True, out
    assert out["unbased"] == [PATH]
    assert f"Land-Unbased: {PATH}" in _git(["log", "-1", "--format=%B"], repo)
    assert _git(["log", "--grep", "Land-Unbased", "--format=%h"], repo)


def test_a_based_land_carries_no_unbased_field_and_no_trailer(repo, monkeypatch):
    out = _land(repo, monkeypatch, run_base=_git(["rev-parse", "HEAD"], repo))

    assert out["committed"] is True, out
    assert "unbased" not in out
    assert "Land-Unbased" not in _git(["log", "-1", "--format=%B"], repo)
