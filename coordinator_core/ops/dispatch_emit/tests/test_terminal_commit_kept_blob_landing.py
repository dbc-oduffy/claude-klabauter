"""dispatch.terminal_commit lands the run's own kept blob for a path a peer also declares, leaving the
peer's hunk in the worktree; a co-declared path with no recorded blob still refuses. Repos live under
tmp_path and are ceilinged so no git call can reach an outer repo."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import kept_work, peer_hunk_gate, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.session import core as session_core, touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c3"
PATH = "pkg/execute_review.py"
SIBLING = "run-sibling"
RUN = "ask-20261027T120000-c3c3c3"
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


def _declare_sibling(repo: Path) -> None:
    rdir = repo / "state" / "warp" / SIBLING
    rdir.mkdir(parents=True)
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": []}), encoding="utf-8"
    )
    run_token.mint(repo, SIBLING, "sibling.mjs", session_id=SELF)


def _record_kept(repo: Path, blob: str) -> None:
    kept_work.write(repo / "state" / "warp" / RUN, {}, None, post_checkpoint={PATH: blob})


def reviewed(root: Path) -> dict:
    sidecar = root.parent / "review-sidecar.md"
    sidecar.write_text("---\nverdict: PASS\n---\n\nreview\n", encoding="utf-8")
    return {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "wave_reviews": [{"slice_id": "s1", "verdict": "PASS", "sidecar_path": str(sidecar)}],
    }


def _fire(repo: Path) -> dict:
    (repo / PATH).write_text(PEER_TEXT, encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(PATH,)),))
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {
        "inline_review": reviewed(repo), "script_path": "run.mjs", "incomplete_chunks": [],
        "session_id": SELF, "run_id": RUN,
    }
    return terminal_commit._handler(params, repo_root=repo / ".git")


def test_a_recorded_kept_blob_lands_and_the_peer_hunk_stays_in_the_worktree(repo):
    _declare_sibling(repo)
    blob = _git(["hash-object", "-w", "--stdin"], repo, stdin=OWN_TEXT)
    _record_kept(repo, blob)

    out = _fire(repo)

    assert out["committed"] is True, out
    assert _git(["rev-parse", f"HEAD:{PATH}"], repo) == blob
    assert (repo / PATH).read_text(encoding="utf-8") == PEER_TEXT


def test_a_co_declared_path_with_no_recorded_blob_lands_whole_naming_the_peer_in_provenance(repo):
    _declare_sibling(repo)
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo)

    assert out["committed"] is True, out
    assert out.get("refused") is None, out
    assert [e["reason"] for e in out["provenance"]] == [peer_hunk_gate.PEER_HUNKS_CO_DECLARED], out
    assert SIBLING in str(out["provenance"]), out
    assert _git(["rev-parse", "HEAD"], repo) != head
    assert _git(["show", f"HEAD:{PATH}"], repo) == PEER_TEXT.strip()
    assert (repo / PATH).read_text(encoding="utf-8") == PEER_TEXT


def test_a_recorded_blob_missing_from_the_object_store_refuses(repo):
    _declare_sibling(repo)
    _record_kept(repo, "a" * 40)
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo)

    assert out["committed"] is False, out
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert (repo / PATH).read_text(encoding="utf-8") == PEER_TEXT


def _record_own_write(repo: Path, text: str) -> None:
    """The run session's latest own write of PATH, as the touch record carries it."""
    (repo / PATH).write_bytes(text.encode())
    sink = touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SELF)
    touch_record.append_event(
        sink, session_id=SELF, agent_id=None, verb="T", path=PATH,
        content_hash=touch_record.compute_content_hash(repo / PATH), kind="w",
    )


def test_a_kept_blob_matching_the_runs_latest_own_write_lands(repo):
    _declare_sibling(repo)
    blob = _git(["hash-object", "-w", "--stdin"], repo, stdin=OWN_TEXT)
    _record_kept(repo, blob)
    _record_own_write(repo, OWN_TEXT)

    out = _fire(repo)

    assert out["committed"] is True, out
    assert _git(["rev-parse", f"HEAD:{PATH}"], repo) == blob
    assert (repo / PATH).read_text(encoding="utf-8") == PEER_TEXT


def test_a_kept_blob_older_than_the_runs_newer_own_write_refuses_and_commits_nothing(repo):
    _declare_sibling(repo)
    blob = _git(["hash-object", "-w", "--stdin"], repo, stdin=OWN_TEXT)
    _record_kept(repo, blob)
    _record_own_write(repo, "x = 3\n")
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo)

    assert out["committed"] is False, out
    assert out["refused"] == terminal_commit.KEPT_BLOB_STALE
    assert out["stale_paths"] == [PATH]
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert (repo / PATH).read_text(encoding="utf-8") == PEER_TEXT


def test_a_recorded_blob_on_a_path_no_peer_declares_lands_the_worktree_bytes(repo):
    blob = _git(["hash-object", "-w", "--stdin"], repo, stdin="x = 9\n")
    _record_kept(repo, blob)

    out = _fire(repo)

    assert out["committed"] is True, out
    assert (repo / PATH).read_text(encoding="utf-8") == PEER_TEXT
    assert _git(["show", f"HEAD:{PATH}"], repo) == PEER_TEXT.strip()
