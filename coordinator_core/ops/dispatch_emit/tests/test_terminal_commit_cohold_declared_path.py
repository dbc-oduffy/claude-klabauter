"""dispatch.terminal_commit refuses up front, naming the holder run and its release call, a declared path an
unreleased halted run withholds; a hold whose kept content is already at HEAD frees that path to land."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from coordinator_core.win_portability import no_console_creationflags

from coordinator_core.ops.dispatch_emit import cohold_refusal, run_token, shipped_claims, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import DELIVERY_NOTE_DIR, RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_commits_delivered_rows import (
    _REVIEWED,
    DLV,
    PLAN,
    _SID,
    RUN,
    _git,
    _verdict,
    _write,
    repo,  # noqa: F401  (fixture)
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

NOTE = f"{DELIVERY_NOTE_DIR}/{RUN}.md"
HEAD_TEXT = "def m():\n    return 1\n"
NEW_TEXT = "def m():\n    return 2\n"
HOLDER = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - 300))}-9a9a9a"


def _call(root, token=None) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C9"], "inline_review": _REVIEWED,
        "session_id": _SID, "ledger_verdict": _verdict({"essential": False}), "run_id": RUN,
    }
    if token:
        params["run_token"] = token
    return terminal_commit._handler(params, repo_root=root / ".git")


def _holder(root, kept_text: str) -> None:
    """A halted run on another sizing that withheld `m.py`, keeping `kept_text`."""
    base = _git(root, "rev-parse", "HEAD").strip()
    kept_blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(root), input=kept_text,
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()
    rec = f"state/warp/{HOLDER}"
    _write(root, "state/sizings/other.yaml", "status: open\n")
    _write(root, f"{rec}/run.json", json.dumps({"sizing_rel": "state/sizings/other.yaml"}))
    _write(root, f"{rec}/manifest.json", json.dumps({"rows": []}))
    _write(root, f"{rec}/halted.json", json.dumps({
        "status": "halted", "sizing_rel": "state/sizings/other.yaml", "run_base_sha": base,
        "run_artifacts": {"withheld": {"m.py": "halt-record-only"}},
    }))
    _write(root, f"{rec}/kept.json", json.dumps({"chunks": {"X1": {"paths": {"m.py": kept_blob}}}, "plan_gate": None}))


def _stage_landing(root) -> None:
    _write(root, "m.py", HEAD_TEXT)
    _git(root, "add", "m.py")
    _git(root, "commit", "-q", "-m", "m at head")
    _write(root, "m.py", NEW_TEXT)
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("a.py", "m.py")),), deliverable_id=DLV, plan_path=PLAN,
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    _write(root, ".gitignore", ".coordinator-local/\n")
    _write(root, NOTE, "# Delivery\n\n## What shipped\n\n- `m.py`\n")


def test_a_declared_path_a_halted_hold_still_withholds_refuses_naming_the_holder(repo, monkeypatch):  # noqa: F811
    from coordinator_core.ops.dispatch_emit import peer_hunk_gate

    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    _stage_landing(repo)
    _holder(repo, "def m():\n    return 3\n")
    token = run_token.mint(repo, RUN, "run.mjs")
    before = _git(repo, "rev-parse", "HEAD")

    out = _call(repo, token)

    assert out["committed"] is False, out
    assert out["refused"] == cohold_refusal.REFUSED_COHOLD
    assert HOLDER in out["error"] and "`m.py`" in out["error"] and "release:" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == before
    assert run_token.admits(repo, RUN, token, "run.mjs") is True


def test_a_hold_whose_kept_content_is_at_head_lands_the_path_without_a_shipped_refusal(repo):  # noqa: F811
    _stage_landing(repo)
    _holder(repo, HEAD_TEXT)

    out = _call(repo)

    assert out.get("refused") not in (cohold_refusal.REFUSED_COHOLD, shipped_claims.SHIPPED_CLAIM_REFUSED), out
    assert HOLDER not in str(out.get("error"))
    if out.get("committed"):
        assert _git(repo, "show", "HEAD:m.py") == NEW_TEXT
    else:
        assert str(out.get("error")).startswith("residue_gate:"), out
