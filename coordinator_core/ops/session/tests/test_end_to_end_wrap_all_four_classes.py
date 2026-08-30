"""
coordinator_core.ops.session.tests.test_end_to_end_wrap_all_four_classes

C7 (docs/plans/2026-08-05-in-process-writers-declare-their-writes.md) — the
plan's closing proof, AC9. This is the chunk that proves the PLAN, not any
one chunk's individual claim: C1-C6 author no part of this file.

Builds a fixture tree containing freshly-written artifacts of the THREE
surviving (of an original four)
in-process-writer classes this plan fixed, each produced by its REAL
writer (never hand-placed — a hand-placed file proves nothing about
whether the writer itself claims what it wrote):

  1. C1 — coordinator_core.dispatch.provision.provision_subagent_sidecar
     (state/subagent-share/<sid>/...)
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
NONE of the four remain dirty afterward.

Negative half, in the SAME tree (both halves or the proof is worthless):
a live peer session's own artifact, written under `state/subagent-share/
<their-id>/` by the SAME real C1 writer (`provision_subagent_sidecar`),
must still be correctly refused by the closing session's wrap — reported
(attributed to the peer, `ownership["peer"]`/`excluded`) but never
committed. A wrap that swept a peer's file would satisfy "leaves none of
the four classes dirty" while being exactly the cross-session sweep
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
from coordinator_core.dispatch.provision import provision_subagent_sidecar
from coordinator_core.ipc import dispatch_message
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import core
from coordinator_core.subagent_sandbox.provision_report import main as provision_report_main
from coordinator_core.win_portability import (
    no_console_creationflags,
    no_console_passthrough_kwargs,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

C1_ELIGIBLE_TYPE = "coordinator:executor"
C2_ELIGIBLE_TYPE = "coordinator:code-reviewer"


def _make_repo(tmp_path: Path) -> Path:
    """Mirrors test_in_process_writer_claim_path._make_repo /
    test_chain_partition_verdict_store_claim_path._make_repo — check=True on
    every fixture-setup git call so a silent setup failure cannot masquerade
    as a passing test. `commit.gpgsign=false` is required here (unlike the
    read-only oracle tests) because this file's wrap ceremony performs a
    REAL `git commit`."""
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




def test_wrap_leaves_none_of_the_four_classes_dirty_and_refuses_peer_artifact(
    tmp_path, monkeypatch, capsys, exercise_suspended_op
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo = _make_repo(repo_root)

    session_id = "sess-c7-closing-session"
    peer_id = "sess-c7-live-peer"
    core.init(session_id, cwd=str(repo))
    core.init(peer_id, cwd=str(repo))

    # -----------------------------------------------------------------
    # Class 1 — dispatch/provision.py's real provision_subagent_sidecar,
    # once for the closing session (mine), once for a live peer (negative
    # half). Same real writer, two different dispatching sessions.
    # -----------------------------------------------------------------
    c1_policy = _write_policy(tmp_path, C1_ELIGIBLE_TYPE)

    c1_mine_rel = provision_subagent_sidecar(
        {"agent_type": C1_ELIGIBLE_TYPE, "session_id": session_id},
        str(c1_policy),
        str(repo),
    )
    assert c1_mine_rel is not None, "fixture failure: C1 sidecar (mine) was not provisioned"
    assert (repo / c1_mine_rel).is_file()

    peer_rel = provision_subagent_sidecar(
        {"agent_type": C1_ELIGIBLE_TYPE, "session_id": peer_id},
        str(c1_policy),
        str(repo),
    )
    assert peer_rel is not None, "fixture failure: peer sidecar was not provisioned"
    assert (repo / peer_rel).is_file()
    assert peer_rel != c1_mine_rel

    # -----------------------------------------------------------------
    # Class 2 — subagent_sandbox/provision_report.py's real CLI `main`,
    # the actual spawn-time entrypoint (stdin JSON payload -> stdout
    # envelope), for the closing session only.
    # -----------------------------------------------------------------
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

    # Class 3 — workstream_complete/chain_partition_verdict_store.py's
    # write_verdict_record — is REMOVED (state/kill-ledger.md K-007,
    # 2026-08-19): the module and the gate that drove it are gone, so the
    # class has no writer left to exercise. Three classes remain; the
    # sweep's contract over them is unchanged.
    # -----------------------------------------------------------------
    # Class 4 — ops/artifact_emit.py's "artifact.emit" op — is REMOVED
    # (state/kill-ledger.md, PM ruling 2026-08-23: CUT IN FULL, and
    # `op_budget_suspension.py` records it "closed forever"). The module,
    # the op and its registration are all gone, so the class has no writer
    # left to exercise -- exactly the disposition Class 3 got above.
    #
    # This leg was left in place when the op was cut and failed on its own
    # import guard ("artifact.emit not registered"), which is the failure
    # mode a string-keyed guard over a killed op always takes: it cannot
    # pass and it names nothing a reader can fix. Removed rather than
    # skipped, so the file does not carry a permanently-red assertion about
    # a mechanism that no longer exists.
    #
    # Two classes remain; the sweep's contract over them is unchanged.
    # -----------------------------------------------------------------

    # -----------------------------------------------------------------
    # Pre-wrap sanity: every one of the four newly-written files (three
    # classes + the peer's) is genuinely dirty in the working tree.
    # -----------------------------------------------------------------
    before = _dirty_status(repo)
    for rel in (c1_mine_rel, c2_rel, peer_rel):
        assert rel in before, f"fixture failure: {rel!r} is not dirty before the wrap"

    # -----------------------------------------------------------------
    # Ownership readout sanity, BEFORE the wrap: all three classes are
    # this session's own claimed work; the peer's own artifact is
    # attributed to the peer, never to "mine".
    # -----------------------------------------------------------------
    offer = safe_commit_offer.compute_offer(session_id, cwd=str(repo))
    for rel in (c1_mine_rel, c2_rel):
        assert rel in offer["safe_paths"], (
            f"{rel!r} did not reach safe_paths: safe_paths={offer['safe_paths']!r} "
            f"orphans={offer['orphans']!r} excluded={offer['excluded']!r}"
        )
    assert peer_rel not in offer["safe_paths"]
    ownership = offer["ownership"]
    assert ownership["degraded"] is False

    # Attribution is asserted through `full_ownership_map`, not through
    # `offer["ownership"]["peer"]`. `compute_offer` builds that bucket from
    # `CommitSet.contested` alone -- paths THIS session claims that a peer
    # claims too -- so a path only the PEER claims is absent from it by
    # construction, and this assertion read `peer: []` forever.
    #
    # The fix is NOT to fold `CommitSet.peers` into `compute_offer`: that
    # collection is sized by the claim ledger (~405 entries on this repo) and
    # its own docstring says to keep it OUT of anything crossing the op wire
    # (~72KB as JSON), in-process consumers only. `full_ownership_map` IS that
    # in-process consumer and already walks `peers` alongside `contested`.
    #
    # The guarantee that actually protects the peer -- their artifact never
    # reaching this session's pathspec -- is asserted directly above, and
    # holds independently of which readout names the owner.
    _mine, peer_map = safe_commit_offer.full_ownership_map(session_id, cwd=str(repo))
    peer_entry = peer_map.get(peer_rel)
    assert peer_entry is not None, (
        f"peer artifact {peer_rel!r} was not attributed to a named peer: "
        f"peer_map={peer_map!r}"
    )
    assert peer_entry["owner"] == peer_id

    # -----------------------------------------------------------------
    # THE PROOF: run the real wrap ceremony for the closing session.
    # -----------------------------------------------------------------
    report = safe_commit_offer.commit_session_offer(session_id, cwd=str(repo))
    assert report["failed_groups"] == [], report["failed_groups"]

    after = _dirty_status(repo)
    for rel in (c1_mine_rel, c2_rel):
        assert rel not in after, (
            f"{rel!r} is STILL dirty after the wrap — the writer's claim did "
            f"not survive to a committable state: git status:\n{after}"
        )

    # -----------------------------------------------------------------
    # THE NEGATIVE HALF: the live peer's artifact is untouched — still
    # dirty (never swept), never committed by this session's wrap.
    # -----------------------------------------------------------------
    assert peer_rel in after, (
        "a live peer's artifact was swept by this session's wrap — exactly "
        f"the cross-session-sweep incident this plan exists to prevent:\n{after}"
    )

    committed_paths = {p for g in report["groups"] for p in g["paths"]}
    assert peer_rel not in committed_paths
    assert {c1_mine_rel, c2_rel} <= committed_paths
