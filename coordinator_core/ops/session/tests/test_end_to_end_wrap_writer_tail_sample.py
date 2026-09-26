
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

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

C1_ELIGIBLE_TYPE = "coordinator:executor"


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
            replan_of="c9-fixture-source",
        )
    baton_rel = baton_result["path"]
    assert (repo / baton_rel).is_file(), "fixture failure: replan baton was not written"

    # DIFFERENT live session. Not one of this plan's four seam entry
    peer_policy = _write_policy(tmp_path, C1_ELIGIBLE_TYPE)
    peer_rel = _provision(
        {"agent_type": C1_ELIGIBLE_TYPE, "session_id": peer_id},
        str(peer_policy),
        str(repo),
    )
    assert peer_rel is not None, "fixture failure: peer sidecar was not provisioned"
    assert (repo / peer_rel).is_file()

    before = _dirty_status(repo)
    for rel in (goal_log_rel, forensics_rel, baton_rel, peer_rel):
        assert rel in before, f"fixture failure: {rel!r} is not dirty before the wrap"

    offer = safe_commit_offer.compute_offer(session_id, cwd=str(repo))
    for rel in (goal_log_rel, forensics_rel, baton_rel):
        assert rel in offer["safe_paths"], (
            f"{rel!r} did not reach safe_paths: safe_paths={offer['safe_paths']!r} "
            f"orphans={offer['orphans']!r} excluded={offer['excluded']!r}"
        )
    assert peer_rel not in offer["safe_paths"]
    assert offer["ownership"]["degraded"] is False

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
    for rel in (goal_log_rel, forensics_rel, baton_rel):
        assert rel not in after, (
            f"{rel!r} is STILL dirty after the wrap — the writer's claim did "
            f"not survive to a committable state: git status:\n{after}"
        )

    # THE NEGATIVE HALF: the live peer's artifact is untouched.
    assert peer_rel in after, (
        "a live peer's artifact was swept by this session's wrap — exactly "
        f"the cross-session-sweep incident this plan exists to prevent:\n{after}"
    )

    committed_paths = {p for g in report["groups"] for p in g["paths"]}
    assert peer_rel not in committed_paths
    assert {goal_log_rel, forensics_rel, baton_rel} <= committed_paths


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
