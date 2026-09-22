"""
coordinator_core.ops.session.tests.test_end_to_end_wrap_writer_tail_sample

C9 (docs/plans/2026-09-11-state-writers-claim-through-one-seam.md) — AC8, the
plan's closing proof. Built from
test_end_to_end_wrap_all_four_classes.py's fixture shape (same helpers,
same two-halves structure), but exercising THIS plan's migrated writer tail
instead of the in-process-writer plan's four classes.

Builds a fixture tree containing freshly-written artifacts, each produced by
its REAL migrated writer (never hand-placed), one from each migration batch:

  1. C5 (ops/) — `coordinator_core.ops.goal_append.append_goal`, the
     `goal.append` op handler's own writer function, reached directly (its
     real entry — the op wraps it via `ipc.dispatch_message`, but the write
     itself lives in this function). Exercises `append_claimed_line`.
  2. C6 (guard planes) —
     `coordinator_core.write_guards.validate_frontmatter_schema_deny.
     _capture_guard_forensics`, reached directly (its real entry — `check()`
     calls it only from the deny/load-failure branches, and this function is
     the one that performs the write). Exercises `replace_text`, which
     delegates to `replace_bytes` internally (see `session/claimed_write.py`),
     so this one call also covers that entry point.
  3. C7 (every other package) —
     `coordinator_core.roadmap.blitz_land.mint_replan_baton`, reached
     directly (its real, public entry point). Exercises `create_exclusive`.

Together these three calls exercise all four seam entry points
(`replace_text`, `replace_bytes`, `create_exclusive`, `append_claimed_line`),
each through a real migrated writer's own call, never the seam functions
invoked directly by this test.

Then runs a real wrap ceremony — `safe_commit_offer.commit_session_offer` —
and asserts NONE of the fixtures remain dirty afterward.

Negative half, in the SAME tree (both halves or the proof is worthless): a
live peer session's own artifact, written under `state/subagent-share/
<their-id>/` by the real `subagent_sandbox.provision_report._provision`
writer (a `touch_written_path` claimant, not one of this plan's four seam
entry points, but still a real claiming writer whose claim belongs to a
different session), must still be correctly refused by the closing
session's wrap — reported but never committed. A wrap that swept a peer's
file would satisfy "leaves none of the fixtures dirty" while being exactly
the cross-session sweep incident the seam plan and the in-process-writer
plan both exist to prevent.

Manual red-check (not automated, per AC8: "watch the test red with one
migrated writer reverted to its raw primitive"): during authoring,
`append_claimed_line(log_file, encoded_row)` in
`coordinator_core/ops/goal_append.py::append_goal` was reverted in place to
`Path(log_file).open("ab").write(encoded_row)`, and this test's post-wrap
"none of the fixtures dirty" assertion for the goal-append fixture went red
(the raw write bypasses the seam, so `commit_session_offer` never sees a
claim for it and the file stays dirty). The revert was then undone; no
permanently-red test is left in the tree — see this chunk's dispatch report
for the observed failure text.

Spec backlink: docs/plans/2026-09-11-state-writers-claim-through-one-seam.md
§ C9 (AC8)
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.cli_entry import recording_declared_writes
from coordinator_core.ops.goal_append import append_goal
from coordinator_core.roadmap.blitz_land import mint_replan_baton
from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import core
from coordinator_core.subagent_sandbox.provision_report import _provision
from coordinator_core.write_guards.validate_frontmatter_schema_deny import (
    _capture_guard_forensics,
)
from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs

# Spawns a real external process (git, via _make_repo/_dirty_status/
# _capture_guard_forensics's own `git status` probe); runs at cadence gates,
# not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

C1_ELIGIBLE_TYPE = "coordinator:executor"


def _make_repo(tmp_path: Path) -> Path:
    """Mirrors test_end_to_end_wrap_all_four_classes.py's `_make_repo` —
    check=True on every fixture-setup git call so a silent setup failure
    cannot masquerade as a passing test. `commit.gpgsign=false` is required
    because this file's wrap ceremony performs a REAL `git commit`."""
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
    import yaml

    path = tmp_path / "subagent-sandbox-policy.yaml"
    path.write_text(
        yaml.safe_dump({"report_sidecar": list(eligible_types)}), encoding="utf-8"
    )
    return path


def test_wrap_leaves_none_of_the_migrated_writer_tail_fixtures_dirty_and_refuses_peer_artifact(
    tmp_path, monkeypatch, exercise_suspended_op
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo = _make_repo(repo_root)

    session_id = "sess-c9-closing-session"
    peer_id = "sess-c9-live-peer"
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    core.init(session_id, cwd=str(repo))
    core.init(peer_id, cwd=str(repo))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", session_id)

    # -----------------------------------------------------------------
    # C5 (ops/) — goal_append.append_goal, real entry. append_claimed_line.
    # -----------------------------------------------------------------
    # `recording_declared_writes` opens the same collection `ipc.dispatch_message`
    # / `run_op_main` would open around a real op call — outside an open
    # collection `declare_write` is a no-op (session/claimed_write.py's own
    # negative spec), so each of the three real writer calls below runs inside
    # one, exactly as it would when invoked through its production entry.
    with recording_declared_writes(cwd=str(repo)):
        goal_result = append_goal(
            period="day",
            period_value="2026-09-19",
            text="C9 writer-tail sample fixture",
            repo="claude-klabauter",
            central_state_root=repo / "state" / "goals",
            hostname="c9-fixture-host",
        )
    goal_log_abs = Path(goal_result["log_file"])
    assert goal_log_abs.is_file(), "fixture failure: goal-append log was not written"
    goal_log_rel = goal_log_abs.relative_to(repo).as_posix()

    # -----------------------------------------------------------------
    # C6 (guard planes) —
    # validate_frontmatter_schema_deny._capture_guard_forensics, real entry
    # (called only from check()'s deny/load-failure branches in production;
    # this is the one function that performs the write). replace_text,
    # which itself delegates to replace_bytes.
    # -----------------------------------------------------------------
    with recording_declared_writes(cwd=str(repo)):
        _capture_guard_forensics(
            payload={"cwd": str(repo), "tool_name": "Write", "tool_input": {"file_path": "x.md"}},
            forensics={"matched_schema_name": None, "schemas_dir": None},
            capture_reason="c9-fixture",
            deny_reason="c9-fixture-deny",
        )
    forensics_dir = repo / "state" / "scratch" / "write-guard-forensics"
    forensics_files = list(forensics_dir.glob("*.json"))
    assert len(forensics_files) == 1, (
        f"fixture failure: expected exactly one forensics dump, found {forensics_files!r}"
    )
    forensics_rel = forensics_files[0].relative_to(repo).as_posix()

    # -----------------------------------------------------------------
    # C7 (every other package) — roadmap.blitz_land.mint_replan_baton, real
    # entry (public function). create_exclusive.
    # -----------------------------------------------------------------
    with recording_declared_writes(cwd=str(repo)):
        baton_result = mint_replan_baton(
            worktree_root=repo,
            source_baton_path="state/handoffs/2026-09-19-source.md",
            brief="C9 writer-tail sample fixture brief.",
            handoff_id="c9-fixture-handoff",
            deliverable_id="c9-fixture-deliverable",
            title="C9 writer tail sample",
            branch="main",
            summary="C9 fixture baton.",
        )
    baton_rel = baton_result["path"]
    assert (repo / baton_rel).is_file(), "fixture failure: replan baton was not written"

    # -----------------------------------------------------------------
    # Peer fixture — a real claiming writer (touch_written_path via
    # subagent_sandbox.provision_report._provision), but claimed by a
    # DIFFERENT live session. Not one of this plan's four seam entry
    # points; included as the negative half's real artifact.
    # -----------------------------------------------------------------
    peer_policy = _write_policy(tmp_path, C1_ELIGIBLE_TYPE)
    peer_rel = _provision(
        {"agent_type": C1_ELIGIBLE_TYPE, "session_id": peer_id},
        str(peer_policy),
        str(repo),
    )
    assert peer_rel is not None, "fixture failure: peer sidecar was not provisioned"
    assert (repo / peer_rel).is_file()

    # -----------------------------------------------------------------
    # Pre-wrap sanity: every fixture is genuinely dirty in the working tree.
    # -----------------------------------------------------------------
    before = _dirty_status(repo)
    for rel in (goal_log_rel, forensics_rel, baton_rel, peer_rel):
        assert rel in before, f"fixture failure: {rel!r} is not dirty before the wrap"

    # -----------------------------------------------------------------
    # Ownership readout sanity, BEFORE the wrap.
    # -----------------------------------------------------------------
    offer = safe_commit_offer.compute_offer(session_id, cwd=str(repo))
    for rel in (goal_log_rel, forensics_rel, baton_rel):
        assert rel in offer["safe_paths"], (
            f"{rel!r} did not reach safe_paths: safe_paths={offer['safe_paths']!r} "
            f"orphans={offer['orphans']!r} excluded={offer['excluded']!r}"
        )
    assert peer_rel not in offer["safe_paths"]
    assert offer["ownership"]["degraded"] is False

    # Peer attribution asserted through full_ownership_map, not
    # offer["ownership"]["peer"] — see test_end_to_end_wrap_all_four_classes.py's
    # identical note: compute_offer's "peer" bucket is built from
    # CommitSet.contested (paths BOTH sessions claim), so a path only the
    # peer claims is absent from it by construction. full_ownership_map
    # walks CommitSet.peers directly.
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
    for rel in (goal_log_rel, forensics_rel, baton_rel):
        assert rel not in after, (
            f"{rel!r} is STILL dirty after the wrap — the writer's claim did "
            f"not survive to a committable state: git status:\n{after}"
        )

    # -----------------------------------------------------------------
    # THE NEGATIVE HALF: the live peer's artifact is untouched.
    # -----------------------------------------------------------------
    assert peer_rel in after, (
        "a live peer's artifact was swept by this session's wrap — exactly "
        f"the cross-session-sweep incident this plan exists to prevent:\n{after}"
    )

    committed_paths = {p for g in report["groups"] for p in g["paths"]}
    assert peer_rel not in committed_paths
    assert {goal_log_rel, forensics_rel, baton_rel} <= committed_paths


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
