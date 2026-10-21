"""A terminal commit's own review-row append to ``<plan>.evidence.yaml`` is never blob-drift, and
a recorded expectation that is not the pre-append bytes still refuses."""

from __future__ import annotations

import hashlib
import subprocess

import pytest

from coordinator_core.ops import criterion_evidence
from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.review_mint import wave_bookkeeping
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

SID = "11111111-2222-3333-4444-555555555555"
PLAN = "docs/plans/ex.md"
EVIDENCE = "docs/plans/ex.evidence.yaml"
SEED = "reviews: []\n"


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fire(tmp_path, monkeypatch, recorded: str) -> tuple:
    """Commit a seeded sidecar, record the session's last write of it at ``recorded``'s bytes, make
    the call's own review-row writer append to it, fire the terminal commit; return (reply,
    commit_v2 params, repo)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / PLAN).write_text("---\nplan_id: pln-ex-abc123\n---\n# Ex\n", encoding="utf-8", newline="")
    (repo / EVIDENCE).write_text(SEED, encoding="utf-8", newline="")
    (repo / "README.md").write_text("seed\n", encoding="utf-8", newline="")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    (repo / "foo.py").write_text("x = 2\n", encoding="utf-8", newline="")
    touch_record.append_event(
        touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SID),
        session_id=SID, agent_id=None, verb="T", path=EVIDENCE,
        content_hash=_sha(recorded), kind="w", timestamp=1.0,
    )

    outside = tmp_path / "record.md"
    outside.write_text("record\n", encoding="utf-8", newline="")
    monkeypatch.setattr(
        wave_bookkeeping, "bookkeep_wave",
        lambda *a, **k: {"sidecar_path": str(outside), "review_firings": [{"leg": "r"}]},
    )

    def append_row(params, repo_root):
        path = repo_root / EVIDENCE
        path.write_text(path.read_text(encoding="utf-8") + "# own row\n", encoding="utf-8", newline="")
        return {}

    monkeypatch.setattr(criterion_evidence, "_criterion_evidence_record", append_row)

    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("foo.py", EVIDENCE)),),
        session_id=SID, plan_path=PLAN,
    )
    (repo / "run.mjs").write_text("// s\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    token = run_token.mint(repo, "run-1", "run.mjs")

    seen: list = []
    real = terminal_commit.reentrant_dispatch

    def spy(op, params, **kw):
        if op == "ceremony.commit_v2":
            seen.append(dict(params))
        return real(op, params, **kw)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", spy)
    out = terminal_commit._handler(
        {
            "script_path": "run.mjs", "incomplete_chunks": [], "session_id": SID,
            "run_id": "run-1", "run_token": token,
            "inline_review": {
                "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
                "prep_sidecar": "prep.md", "plan_id": "pln-ex-abc123",
                "wave_sidecar_paths": ["w.md"],
            },
        },
        repo_root=repo / ".git",
    )
    return out, seen, repo


def test_the_runs_own_review_row_append_commits_the_sidecar(tmp_path, monkeypatch):
    out, seen, repo = _fire(tmp_path, monkeypatch, SEED)
    assert out["committed"] is True, out
    final = (repo / EVIDENCE).read_bytes()
    assert final != SEED.encode()
    assert seen[0]["expected_content_sha256"][EVIDENCE] == hashlib.sha256(final).hexdigest()


def test_a_recorded_write_that_is_not_the_pre_append_bytes_still_refuses(tmp_path, monkeypatch):
    out, seen, _ = _fire(tmp_path, monkeypatch, "reviews: [peer]\n")
    assert out["committed"] is False, out
    assert out["refused"] == "blob-drift"
    assert out["blob_drift"][0]["path"] == EVIDENCE
    assert seen[0]["expected_content_sha256"][EVIDENCE] == _sha("reviews: [peer]\n")
