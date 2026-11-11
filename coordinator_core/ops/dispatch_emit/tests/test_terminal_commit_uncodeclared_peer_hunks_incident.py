"""The 7253f0 sweep through the default `dispatch.terminal_commit` entry.

A run lands a shared path that also carries a live peer's hunk. The peer holds no manifest (or a live
one the reviewer names). The landing never takes the peer's hunk into HEAD; a path the peer's hunk
cannot be split from is withheld naming the peer; a path with no foreign hunk lands whole.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    ManifestRow,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c2"
P = "pkg/ask_compose.py"
LINES = 120
PEER_LINE = 5
OWN_LINE = 115
RUN_OWN = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-7253f0"
RUN_PEER = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - 60))}-594963"
PEER_MARK = f"x_{PEER_LINE} = 'PEER-EDIT'"
OWN_MARK = f"x_{OWN_LINE} = 'OWN-EDIT'"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, check=True, **no_console_creationflags(),
    ).stdout.decode("utf-8").strip()


def _body(peer: bool = False, own: bool = False) -> str:
    rows = [f"x_{i} = {i}" for i in range(1, LINES + 1)]
    if peer:
        rows[PEER_LINE - 1] = PEER_MARK
    if own:
        rows[OWN_LINE - 1] = OWN_MARK
    return "\n".join(rows) + "\n"


def _declare(root: Path, run_id: str, baseline: dict | None) -> None:
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{run_id}",
        rows=(ManifestRow(
            id="C1", agent_type="coordinator:executor", model="sonnet", brief_path="b.md",
            writes=(P,), wave=0,
        ),),
        review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{run_id}/commit-request.txt",
        write_baseline=baseline or {},
    )
    (rdir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")


def _build(tmp_path: Path, monkeypatch, *, peer_hunk: bool, peer_declares: bool = False) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "pkg").mkdir()
    (root / P).write_text(_body(), encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    if peer_hunk:
        (root / P).write_text(_body(peer=True), encoding="utf-8")
    if peer_declares:
        _declare(root, RUN_PEER, None)
        run_token.mint(root, RUN_PEER, "peer.mjs", session_id=SELF)
    _declare(root, RUN_OWN, wb.capture(root, [P]))
    run_token.mint(root, RUN_OWN, "run.mjs", session_id=SELF)
    (root / P).write_text(_body(peer=peer_hunk, own=True), encoding="utf-8")
    return root


def _fire(root: Path) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(P,)),))
    marker = root / RUN_DIR_ROOT / RUN_OWN / "commit-request.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(render_marker(request) + "\n", encoding="utf-8")
    (root / "run.mjs").write_text(
        f"{ASK_MANIFEST_MARKER}state/warp/{RUN_OWN}/manifest.json\n", encoding="utf-8"
    )
    params = {
        "inline_review": reviewed(), "script_path": "run.mjs", "incomplete_chunks": [],
        "run_id": RUN_OWN, "session_id": SELF,
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


def _flag_out_of_ask(root: Path) -> None:
    share = root / ".coordinator-local" / "subagent-share" / SELF
    share.mkdir(parents=True)
    (share / f"{RUN_OWN}.review-wave-bookkeeping.md").write_text(
        '---\nintegrated_from: ["rev-1"]\n---\n\nbody\n', encoding="utf-8"
    )
    row = {"id": "finding-1", "file": P, "summary": "ask_compose.py carries hunks of other asks",
           "out_of_ask": True}
    (share / "rev-1.md").write_text(
        "---\nagent_type: reviewer\n---\n\n## Findings\n\n### Finding 1\n\nOut-of-ask hunks.\n"
        f"\n## Findings Ledger\n\n```json\n{json.dumps([row])}\n```\n",
        encoding="utf-8",
    )


def test_an_undeclared_live_peers_hunk_in_the_landing_path_is_never_committed(tmp_path, monkeypatch):
    root = _build(tmp_path, monkeypatch, peer_hunk=True)
    seed_blob = _git(["show", f"HEAD:{P}"], root)

    out = _fire(root)

    assert out["committed"] is False, out
    assert P in (out.get("sibling_withheld") or {}), out
    assert _git(["show", f"HEAD:{P}"], root) == seed_blob
    worktree = (root / P).read_text(encoding="utf-8")
    assert PEER_MARK in worktree and OWN_MARK in worktree


def test_an_out_of_ask_finding_on_a_path_with_a_live_declaring_peer_withholds_it_naming_the_peer(
    tmp_path, monkeypatch,
):
    root = _build(tmp_path, monkeypatch, peer_hunk=True, peer_declares=True)
    _flag_out_of_ask(root)
    seed = _git(["rev-parse", "HEAD"], root)

    out = _fire(root)

    assert out["committed"] is False, out
    assert out["sibling_withheld"][P] == RUN_PEER, out
    assert _git(["rev-parse", "HEAD"], root) == seed
    assert PEER_MARK in (root / P).read_text(encoding="utf-8")


def test_an_out_of_ask_finding_with_an_undeclared_peer_withholds_it_naming_a_run_or_the_reviewer(
    tmp_path, monkeypatch,
):
    root = _build(tmp_path, monkeypatch, peer_hunk=True)
    _flag_out_of_ask(root)
    seed = _git(["rev-parse", "HEAD"], root)

    out = _fire(root)

    assert out["committed"] is False, out
    assert out["sibling_withheld"][P], out
    assert _git(["rev-parse", "HEAD"], root) == seed
    assert PEER_MARK in (root / P).read_text(encoding="utf-8")


def test_a_landing_with_no_foreign_hunk_and_no_finding_commits_the_whole_file(tmp_path, monkeypatch):
    root = _build(tmp_path, monkeypatch, peer_hunk=False)

    out = _fire(root)

    assert out["committed"] is True, out
    assert not out.get("sibling_withheld"), out
    assert _git(["show", f"HEAD:{P}"], root) == _body(own=True).strip()
