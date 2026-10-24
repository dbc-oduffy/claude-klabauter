"""dispatch.terminal_commit refuses a landing whose declared path a co-declaring live run also
claims, or that an unresolved reviewer out-of-ask finding names."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import peer_hunk_gate, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c3"
PATH = "pkg/execute_review.py"
SIBLING = "run-sibling"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
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


def _fire(repo: Path) -> dict:
    (repo / PATH).write_text("x = 2\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(PATH,)),))
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {"inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": [], "session_id": SELF}
    return terminal_commit._handler(params, repo_root=repo / ".git")


def test_a_path_a_live_sibling_run_also_declares_is_refused_naming_it(repo):
    _declare_sibling(repo)
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo)

    assert out["committed"] is False, out
    assert out["refused"] == peer_hunk_gate.PEER_HUNKS_CO_DECLARED
    assert out["integrity"][0]["path"] == PATH
    assert SIBLING in out["integrity"][0]["detail"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_the_same_landing_without_a_co_declaring_sibling_commits(repo):
    out = _fire(repo)

    assert out["committed"] is True, out


def test_an_unresolved_out_of_ask_finding_on_the_path_is_refused(repo):
    share = repo / ".coordinator-local" / "subagent-share" / SELF
    share.mkdir(parents=True)
    (share / "run.review-wave-bookkeeping.md").write_text(
        '---\nintegrated_from: ["rev-1"]\n---\n\nbody\n', encoding="utf-8"
    )
    (share / "rev-1.md").write_text(
        "---\nagent_type: reviewer\n---\n\n## Findings\n\n### Finding 1\n\n"
        "Out-of-ask hunks: failing_detail changes in execute_review.py belong to another run.\n",
        encoding="utf-8",
    )
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo)

    assert out["committed"] is False, out
    assert out["refused"] == peer_hunk_gate.OUT_OF_ASK_HUNKS
    assert "rev-1" in out["integrity"][0]["detail"]
    assert _git(["rev-parse", "HEAD"], repo) == head
