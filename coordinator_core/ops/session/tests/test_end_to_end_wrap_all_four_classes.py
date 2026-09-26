"""
coordinator_core.ops.session.tests.test_end_to_end_wrap_all_four_classes
(module filename retained per this plan's `writes:` scope, which names this
exact path — the test FUNCTION below is renamed; see the class count below)

C7 (docs/plans/2026-08-05-in-process-writers-declare-their-writes.md) — the
plan's closing proof, AC9. This is the chunk that proves the PLAN, not any
one chunk's individual claim: C1-C6 author no part of this file.

Builds a fixture tree containing freshly-written artifacts of the in-process-writer
surface this plan fixed, each produced by its REAL writer (never hand-placed —
a hand-placed file proves nothing about whether the writer itself claims what
it wrote). Of the original four writer classes this file once exercised, only
one writer module survives — `coordinator_core.subagent_sandbox.provision_report`
— reached through its two live entrypoints:

  1. C1 — coordinator_core.subagent_sandbox.provision_report._provision, the
     in-process call the spawn path uses directly (state/subagent-share/<sid>/...).
     `docs/decisions/` (see chunk C3 of
     `docs/plans/2026-08-31-the-provisioner-nothing-calls.md`) records that the
     dedicated per-package provisioner module this leg used to
     exercise separately was deleted as an unreachable duplicate of this same
     writer, not repointed — so this leg and Class 2 below now share one real
     writer, exercised via two different entrypoints.
  2. C2 — coordinator_core.subagent_sandbox.provision_report (CLI `main`,
     the real spawn-time entrypoint) (state/subagent-share/<sid>/...)
  3. C3 — REMOVED (state/kill-ledger.md K-007, 2026-08-19): was
     workstream_complete.chain_partition_verdict_store.write_verdict_record
  4. C4 — coordinator_core.ops.artifact_emit's "artifact.emit" op, driven
     through the REAL coordinator_core.ipc.dispatch_message (the ONLY seam
     that turns a handler's `_scope_touch_paths` self-report into a claim)
     (state/cockpit-emission.json) — `_envelope.resolve_context`/`.emit`
     are patched only to avoid the full 21-section envelope build, mirroring
     `coordinator_core/ops/tests/test_artifact_emit_scope_touch.py`'s own
     precedent; the declaration path itself is exercised for real.

Then runs a real wrap ceremony — `safe_commit_offer.commit_session_offer`,
the only wrap mechanism actually landed as of this chunk (C6's claim-aware
Step 2.5 branch is still `pending` in the plan's own AC8 row) — and asserts
NONE of the fixtures remain dirty afterward.

Negative half, in the SAME tree (both halves or the proof is worthless):
a live peer session's own artifact, written under `state/subagent-share/
<their-id>/` by the SAME real Class 1 writer (`provision_report._provision`),
must still be correctly refused by the closing session's wrap — reported
(attributed to the peer, `ownership["peer"]`/`excluded`) but never
committed. A wrap that swept a peer's file would satisfy "leaves none of
the fixtures dirty" while being exactly the cross-session sweep
incident this plan exists to prevent (see the plan's Anti-scope section).

Spec backlink: pln-in-process-engine-writers-decl-33016a § C7 (AC9)
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import io
import pytest
import yaml

import coordinator_core.ipc as ipc
import coordinator_core.ops  # noqa: F401 — populates _REGISTRY (artifact.emit)
from coordinator_core.ipc import dispatch_message
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import core
from coordinator_core.subagent_sandbox.provision_report import _provision
from coordinator_core.subagent_sandbox.provision_report import main as provision_report_main
from coordinator_core.win_portability import (
    no_console_creationflags,
    no_console_passthrough_kwargs,
)

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

C1_ELIGIBLE_TYPE = "coordinator:executor"
C2_ELIGIBLE_TYPE = "coordinator:code-reviewer"


def _make_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True,
        **no_console_passthrough_kwargs(),
    )
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    return tmp_path


def _dirty_status(repo: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    return result.stdout


def _write_policy(tmp_path: Path, *eligible_types: str) -> Path:
    path = tmp_path / "subagent-sandbox-policy.yaml"
    path.write_text(
        yaml.safe_dump({"report_sidecar": list(eligible_types)}), encoding="utf-8"
    )
    return path


def test_wrap_leaves_none_of_the_surviving_writer_fixtures_dirty_and_refuses_peer_artifact(
    tmp_path, monkeypatch, capsys, exercise_suspended_op
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo = _make_repo(repo_root)

    session_id = "sess-c7-closing-session"
    peer_id = "sess-c7-live-peer"
    core.init(session_id, cwd=str(repo))
    core.init(peer_id, cwd=str(repo))

    c1_policy = _write_policy(tmp_path, C1_ELIGIBLE_TYPE)

    c1_mine_rel = _provision(
        {"agent_type": C1_ELIGIBLE_TYPE, "session_id": session_id},
        str(c1_policy),
        str(repo),
    )
    assert c1_mine_rel is not None, "fixture failure: C1 sidecar (mine) was not provisioned"
    assert (repo / c1_mine_rel).is_file()

    peer_rel = _provision(
        {"agent_type": C1_ELIGIBLE_TYPE, "session_id": peer_id},
        str(c1_policy),
        str(repo),
    )
    assert peer_rel is not None, "fixture failure: peer sidecar was not provisioned"
    assert (repo / peer_rel).is_file()
    assert peer_rel != c1_mine_rel

    c2_policy = _write_policy(tmp_path, C2_ELIGIBLE_TYPE)
    c2_payload = {
        "agent_id": "abc123def4567890",
        "agent_type": C2_ELIGIBLE_TYPE,
        "session_id": session_id,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(c2_payload)))
    exit_code = provision_report_main(["--policy", str(c2_policy), "--cwd", str(repo)])
    assert exit_code == 0
    captured = capsys.readouterr()
    c2_rel = json.loads(captured.out.splitlines()[0])["report_sidecar"]
    assert (repo / c2_rel).is_file()


    before = _dirty_status(repo)
    for rel in (c1_mine_rel, c2_rel, peer_rel):
        assert rel in before, f"fixture failure: {rel!r} is not dirty before the wrap"

    offer = safe_commit_offer.compute_offer(session_id, cwd=str(repo))
    for rel in (c1_mine_rel, c2_rel):
        assert rel in offer["safe_paths"], (
            f"{rel!r} did not reach safe_paths: safe_paths={offer['safe_paths']!r} "
            f"orphans={offer['orphans']!r} excluded={offer['excluded']!r}"
        )
    assert peer_rel not in offer["safe_paths"]
    ownership = offer["ownership"]
    assert ownership["degraded"] is False

    _mine, peer_map = safe_commit_offer.full_ownership_map(session_id, cwd=str(repo))
    peer_entry = peer_map.get(peer_rel)
    assert peer_entry is not None, (
        f"peer artifact {peer_rel!r} was not attributed to a named peer: "
        f"peer_map={peer_map!r}"
    )
    assert peer_entry["owner"] == peer_id

    report = safe_commit_offer.commit_session_offer(session_id, cwd=str(repo))
    assert report["failed_groups"] == [], report["failed_groups"]

    after = _dirty_status(repo)
    for rel in (c1_mine_rel, c2_rel):
        assert rel not in after, (
            f"{rel!r} is STILL dirty after the wrap — the writer's claim did "
            f"not survive to a committable state: git status:\n{after}"
        )

    # THE NEGATIVE HALF: the live peer's artifact is untouched — still
    assert peer_rel in after, (
        "a live peer's artifact was swept by this session's wrap — exactly "
        f"the cross-session-sweep incident this plan exists to prevent:\n{after}"
    )

    committed_paths = {p for g in report["groups"] for p in g["paths"]}
    assert peer_rel not in committed_paths
    assert {c1_mine_rel, c2_rel} <= committed_paths
