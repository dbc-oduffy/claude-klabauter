"""The close block's run_id derives from the run being closed, not from a stale script stem a refire passes."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import PurePosixPath

import pytest

from coordinator_core.ops.dispatch_emit import run_close, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.terminal_commit import _close_block
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import (  # noqa: F401 -- repo is a fixture
    _author,
    _declare,
    _land,
    _record_dir,
    repo,
)
from coordinator_core.ops.review_mint import wave_bookkeeping
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

SESSION = "09000527-8268-46ff-82e8-3f7022eb3b00"
FIRST = "ask-20261023T043854-eeb376"
REFIRE = "ask-20261023T050451-4fb340"


def _stem(script: str) -> str:
    return PurePosixPath(script).stem


def _block(tmp_path, run_id: str, script: str) -> dict:
    ctx = {
        "legs": {}, "applies": True, "presets": {}, "kind": "plan-run", "root": tmp_path,
        "plan_rel": "docs/plans/x.md", "completion_entry": None, "session_id": SESSION,
        "run_id": run_close.own_close_key(run_id, _stem(script)),
    }
    return _close_block(ctx, {"committed": False, "nothing_to_commit": True})


def test_two_runs_sharing_a_session_each_close_under_their_own_id(tmp_path):
    first = _block(tmp_path, FIRST, f"scratch/warp/{FIRST}.workflow.js")
    refire = _block(tmp_path, REFIRE, f"scratch/warp/{FIRST}.workflow.js")
    assert first["run_id"] == f"{FIRST}.workflow"
    assert refire["run_id"] == f"{REFIRE}.workflow"


def test_refire_passing_the_other_runs_script_is_rekeyed_to_its_own_run():
    assert run_close.own_close_key(REFIRE, f"{FIRST}.workflow") == f"{REFIRE}.workflow"
    assert run_close.own_close_key(f"{REFIRE}.workflow", f"{FIRST}.workflow") == f"{REFIRE}.workflow"


def test_matching_or_non_workflow_stem_is_left_alone():
    assert run_close.own_close_key(FIRST, f"{FIRST}.workflow") == f"{FIRST}.workflow"
    assert run_close.own_close_key("r1", "plain-script") == "plain-script"
    assert run_close.own_close_key("", f"{FIRST}.workflow") == f"{FIRST}.workflow"


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


@pytest.mark.spawns_process
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


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_land_run_handler_keys_the_close_to_its_own_run_not_the_stale_script(repo):  # noqa: F811
    _author(repo, "src/a.py", "a = 1\n")
    _declare(repo, "src/a.py")
    _record_dir(repo)
    stale = f"scratch/warp/{FIRST}.workflow.js"
    (repo / stale).parent.mkdir(parents=True, exist_ok=True)
    (repo / stale).write_text((repo / "run-a.mjs").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")

    out = _land(repo, script_path=stale, run_id=REFIRE)

    assert out["committed"] is True, out
    assert out["close"]["run_id"] == f"{REFIRE}.workflow"
    assert run_close._job_file(repo, f"{REFIRE}.workflow").is_file()
    assert not run_close._job_file(repo, f"{FIRST}.workflow").exists()
