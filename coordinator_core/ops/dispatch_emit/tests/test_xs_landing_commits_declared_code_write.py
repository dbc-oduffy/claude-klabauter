"""An xs landing whose row declares a code write and its test commits both, or names the held code write.

Models warp-2026-11-01-door-write-floor-deny-shadowed-25c526: the row (no ``register_rows``, as an xs spine
mints it) declared ``engine.py`` and its test; a live peer held ``engine.py``. The landing committed the test,
rode ``Row-Unlanded`` for the code, and still read the ledger item ``met`` because no register row or
``claimed_by`` tied the unlanded row to the item.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit import terminal_commit_ledger as tcl
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    ManifestRow,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000f1"
RUN = "ask-20261101T182112-25c526"
CODE = "pkg/engine.py"
TEST = "pkg/tests/test_engine.py"
REPORT = "reports/X1.md"
VERDICT = {"items": [{"id": "R1", "status": "met", "essential": True, "wired": True}]}


def test_item_with_no_register_row_or_claim_is_served_by_the_unattributed_row():
    got = tcl.downgrade_unlanded_items(VERDICT, [("X1", CODE, "peer-held")], {"X1": ()})

    assert got["items"][0]["status"] == "partial"
    assert got["items"][0]["remaining"] == [CODE]


def test_unattributed_row_that_landed_leaves_the_item_met():
    assert tcl.downgrade_unlanded_items(VERDICT, [("docs", "d.md", "peer-held")], {"X1": ()}) is VERDICT


def test_an_attributed_row_does_not_serve_an_unrelated_item():
    verdict = {"items": [{"id": "R9", "status": "met"}]}
    assert tcl.downgrade_unlanded_items(verdict, [("X1", CODE, "peer-held")], {"X1": ("R1",)}) is verdict


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _claim(root: Path, rel: str) -> None:
    session_scope.touch(SELF, rel, cwd=str(root))
    touch_record.append_event(
        touch_record.sink_path(root / ".git" / "coordinator-sessions" / SELF),
        session_id=SELF, agent_id=None, verb="T", path=rel,
        content_hash=touch_record.compute_content_hash(root / rel), kind="w",
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.autocrlf", "false")
    _write(root, ".gitignore", "scratch/\n.coordinator-local/\n")
    _write(root, "README.md", "seed\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _land(root: Path, *, peer_holds_code: bool) -> dict:
    _write(root, CODE, "x = 1\n")
    _write(root, TEST, "def test_x():\n    assert True\n")
    _write(root, REPORT, "DONE: X1\n")
    for rel in (CODE, TEST):
        _claim(root, rel)
    if peer_holds_code:
        _write(root, "state/warp/run-b/manifest.json", json.dumps({"rows": [{"id": "X1", "writes": [CODE]}]}))
        run_token.mint(root, "run-b", "b.mjs", session_id=SELF)
    run_dir = f"{RUN_DIR_ROOT}/{RUN}"
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=(CODE, TEST), report=REPORT),), plan_path=None,
    )
    from coordinator_core.ops.dispatch_emit.commit_request import render_marker

    _write(root, f"{run_dir}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=run_dir,
        rows=(ManifestRow(
            id="X1", agent_type="coordinator:executor", model="sonnet", brief_path="b.md",
            writes=(CODE, TEST), wave=0,
        ),),
        review_declared_paths=(CODE, TEST), review_code_paths=(CODE, TEST),
        marker_path=f"{run_dir}/commit-request.txt",
    )
    _write(root, f"{run_dir}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{run_dir}/manifest.json\n")
    return land_run._handler(
        {
            "script_path": f"{RUN}.mjs", "incomplete_chunks": [], "session_id": SELF, "run_id": RUN,
            "ledger_verdict": VERDICT,
            "inline_review": {**reviewed(), "integration_stem": "s",
                              "criterion": {"status": "met", "observation": "obs", "sidecar": "j.md"}},
        },
        repo_root=root / ".git",
    )


@pytest.mark.spawns_process
def test_a_peer_held_code_write_is_named_withheld_and_the_item_reads_partial(repo):
    out = _land(repo, peer_holds_code=True)

    assert out["committed"] is True, out
    landed = set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())
    assert TEST in landed and CODE not in landed
    assert out["stranded"] == {"X1": [CODE]}, out
    assert [h["path"] for h in out["peer_held_left"]] == [CODE], out
    assert "peer-held" in out["incomplete_reasons"]["X1"] and CODE in out["incomplete_reasons"]["X1"], out
    assert list(out["ledger_partial"]) == ["R1"], out
    assert out["criterion_status"] == "not_met", out


@pytest.mark.spawns_process
def test_both_declared_writes_land_in_one_commit_and_the_item_stays_met(repo):
    out = _land(repo, peer_holds_code=False)

    assert out["committed"] is True, out
    landed = set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())
    assert {CODE, TEST} <= landed
    assert "ledger_partial" not in out and "peer_held_left" not in out and not out.get("stranded"), out
    assert not _git(repo, "status", "--porcelain", "--", CODE, TEST).strip()
