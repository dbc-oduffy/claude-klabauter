"""A ledger resume whose blocking row is met_by a named pre-halt commit owes no since-halt diff and lands close and bookkeeping paths only."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RUN_DIR_ROOT,
    UNCHANGED_SINCE_HALT_REASON,
    ManifestRow,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import (  # noqa: F401
    _REVIEWED,
    _SID,
    PLAN,
    RUN2,
    SCRIPT2,
    _second_run,
    halted,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import DLV, _write, repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

CREDIT_SHA = "2fd9d96c64"


def _git(root, *a):
    out = subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return out.stdout.strip()


def _verdict(met_by=CREDIT_SHA):
    return {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": met_by}]}


def _resume(halt, **extra):
    return {
        "run_id": "old", "halt_sha": halt, "blocking": ["R1"], "unlanded": ["C2"],
        "revise_notes": [f"R1 met by {CREDIT_SHA}, 15 tests pass"], **extra,
    }


@pytest.fixture
def plain_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "prod.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "halt")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "C1", "writes": ["prod.py"]}]}), encoding="utf-8")
    return tmp_path, manifest, _git(tmp_path, "rev-parse", "HEAD")


def test_credited_row_is_exempt_without_a_git_spawn(plain_repo, monkeypatch):
    root, manifest, halt = plain_repo

    def boom(*a, **k):
        raise AssertionError("git spawned")

    monkeypatch.setattr(tc, "run_git", boom)
    assert tc._unchanged_since_halt_failures(root, _resume(halt), _verdict(), manifest) == []


def test_credited_row_is_exempt_with_declared_path_unchanged(plain_repo):
    root, manifest, halt = plain_repo
    assert tc._unchanged_since_halt_failures(root, _resume(halt), _verdict(), manifest) == []


def test_met_by_naming_an_unnamed_sha_still_refuses(plain_repo):
    root, manifest, halt = plain_repo
    got = tc._unchanged_since_halt_failures(root, _resume(halt), _verdict("abcdef1234"), manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def test_post_commit_row_met_by_the_halt_sha_is_exempt(plain_repo):
    root, manifest, halt = plain_repo
    resume = _resume(halt, revise_notes=[f"R1 met by {halt[:10]} (halt commit)"])
    verdict = {"items": [{"id": "R1", "status": "post_commit", "met_by": [halt[:10]]}]}
    assert tc._unchanged_since_halt_failures(root, resume, verdict, manifest) == []


def test_post_commit_row_with_an_unnamed_sha_still_refuses(plain_repo):
    root, manifest, halt = plain_repo
    verdict = {"items": [{"id": "R1", "status": "post_commit", "met_by": ["abcdef1234"]}]}
    got = tc._unchanged_since_halt_failures(root, _resume(halt), verdict, manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def test_post_commit_row_with_a_noted_sha_that_does_not_resolve_is_refused(plain_repo):
    root, manifest, halt = plain_repo
    verdict = {"items": [{"id": "R1", "status": "post_commit", "met_by": [CREDIT_SHA]}]}
    got = tc._unchanged_since_halt_failures(root, _resume(halt), verdict, manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def test_no_revise_note_still_refuses(plain_repo):
    root, manifest, halt = plain_repo
    got = tc._unchanged_since_halt_failures(root, _resume(halt, revise_notes=[]), _verdict(), manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def _close_only_run(root):
    """Run RUN2 declaring a.py (landed before the halt, unchanged since) while its commit request
    carries only the docs chunk."""
    token = _second_run(root)
    manifest = root / RUN_DIR_ROOT / RUN2 / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["rows"] = [
        ManifestRow(id="C1", agent_type="x", model="m", brief_path="b.md", writes=("a.py",), wave=1).to_json()
    ]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="docs", title="run docs", paths=("docs/note.md",)),),
        deliverable_id=DLV, plan_path=PLAN,
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN2}/commit-request.txt", render_marker(request) + "\n")
    return token


def _terminal(root, token, resume, verdict):
    return tc._handler(
        {
            "script_path": SCRIPT2, "run_id": RUN2, "run_token": token, "incomplete_chunks": [],
            "inline_review": _REVIEWED, "session_id": _SID, "resume_ledger": resume,
            "ledger_verdict": verdict,
        },
        repo_root=root / ".git",
    )


def test_ledger_gate_passes_for_the_credited_resume(halted):
    _close_only_run(halted)
    root = halted
    run_dir = root / RUN_DIR_ROOT / RUN2
    halt = _git(root, "rev-parse", "HEAD")
    refusal = tc._ledger_gate(
        root, run_dir, _verdict(), run_dir / "manifest.json", resume_ledger=_resume(halt)
    )[0]
    assert refusal is None, refusal


def test_close_only_commit_lands_without_a_declared_product_path(halted):
    token = _close_only_run(halted)
    root = halted
    halt = _git(root, "rev-parse", "HEAD")
    out = _terminal(root, token, _resume(halt), _verdict())
    assert out["committed"] is True, out
    landed = _git(root, "diff", "--name-only", f"{halt}..HEAD").splitlines()
    assert landed and "a.py" not in landed
    assert "docs/plans/p.md" in landed


def test_close_only_commit_with_an_uncredited_sha_is_refused(halted):
    token = _close_only_run(halted)
    root = halted
    halt = _git(root, "rev-parse", "HEAD")
    out = _terminal(root, token, _resume(halt), _verdict("abcdef1234"))
    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert out["ledger_failures"] == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]
