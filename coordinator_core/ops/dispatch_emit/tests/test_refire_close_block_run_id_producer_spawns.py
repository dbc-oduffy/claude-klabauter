"""The terminal-commit handler keys the close to its own run, not a stale script stem (real git)."""

from __future__ import annotations

import hashlib
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests.test_refire_close_block_run_id_producer import FIRST, REFIRE, SESSION
from coordinator_core.ops.review_mint import wave_bookkeeping
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def test_terminal_commit_handler_keys_the_close_to_its_own_run_not_the_stale_script(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "foo.py").write_text("seed\n", encoding="utf-8", newline="")
    (root / "docs/plans").mkdir(parents=True)
    (root / "docs/plans/ex.md").write_text(
        "---\nplan_id: pln-ex-abc123\n---\n# Ex\n", encoding="utf-8", newline=""
    )
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "foo.py").write_text("seed\n# edit\n", encoding="utf-8", newline="")
    touch_record.append_event(
        touch_record.sink_path(root / ".git" / "coordinator-sessions" / SESSION),
        session_id=SESSION, agent_id=None, verb="T", path="foo.py",
        content_hash=hashlib.sha256(b"seed\n# edit\n").hexdigest(), kind="w", timestamp=9.0,
    )
    script = f"scratch/warp/{FIRST}.workflow.js"
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("foo.py",)),),
        session_id=SESSION, plan_path="docs/plans/ex.md",
    )
    (root / script).parent.mkdir(parents=True)
    (root / script).write_text("// s\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    token = run_token.mint(root, REFIRE, script)
    outside = tmp_path / "record.md"
    outside.write_text("record\n", encoding="utf-8", newline="")
    monkeypatch.setattr(
        wave_bookkeeping, "bookkeep_wave",
        lambda *a, **k: {"sidecar_path": str(outside), "review_firings": [{"leg": "r"}]},
    )
    seen: list = []
    real = terminal_commit.reentrant_dispatch

    def spy(op, params, **kw):
        seen.append(op)
        return real(op, params, **kw)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", spy)
    out = terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": [], "session_id": SESSION, "run_id": REFIRE,
         "run_token": token,
         "inline_review": {
             "integration_stem": "rev-stem", "slices": 1, "fixes": 0, "prep_sidecar": "prep.md",
             "plan_id": "pln-ex-abc123", "wave_sidecar_paths": ["w.md"],
         }},
        repo_root=root / ".git",
    )
    assert "ceremony.commit_v2" in seen
    assert out["close"]["run_id"] == f"{REFIRE}.workflow"
