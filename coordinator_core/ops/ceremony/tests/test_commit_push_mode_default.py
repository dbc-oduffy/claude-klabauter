"""
coordinator_core.ops.ceremony.tests.test_commit_push_mode_default

C2 (docs/plans/2026-08-19-the-engine-stops-paying-a-network-push-on-every-
commit.md § C2): `run_commit_pipeline`'s pre-push and push legs are each
wrapped in `record_composition_span` (the existing span writer -- no new
sink, no new field), so the op-latency sink carries the pre-push/push split
directly instead of it being attributed by shape.

Runs against a synthetic throwaway `tmp_path` repo only -- never this repo
or the publish mirror, per this chunk's own stated test surface.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.commit_pipeline import (
    _COMPOSITION_SPAN_PRE_PUSH,
    _COMPOSITION_SPAN_PUSH,
    PUSH_MODE_SYNC,
    run_commit_pipeline,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _unique_session_id() -> str:
    return f"test-session-{uuid.uuid4().hex[:8]}"


def _composition_rows(repo: Path) -> list:
    sink_dir = repo / ".git" / "coordinator-sessions" / "logs"
    rows = []
    for sink in sorted(sink_dir.glob("op-latency*.jsonl")):
        for line in sink.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return [r for r in rows if r.get("kind") == "composition"]


def test_pipeline_emits_both_pre_push_and_push_spans(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("seed", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    (repo / "tasks_feature_todo.md").write_text("content", encoding="utf-8")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="commit for span coverage",
        stage_paths=["tasks_feature_todo.md"],
        caller_paths={"tasks_feature_todo.md"},
        push_mode=PUSH_MODE_SYNC,
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None

    rows = _composition_rows(repo)
    names = [r["name"] for r in rows]
    assert names.count(_COMPOSITION_SPAN_PRE_PUSH) == 1
    assert names.count(_COMPOSITION_SPAN_PUSH) == 1

    pre_push_row = next(r for r in rows if r["name"] == _COMPOSITION_SPAN_PRE_PUSH)
    push_row = next(r for r in rows if r["name"] == _COMPOSITION_SPAN_PUSH)

    # Both legs of the SAME invocation share one composition_id -- what makes
    # the two rows joinable as one call's split.
    assert pre_push_row["composition_id"] == push_row["composition_id"]

    for row in (pre_push_row, push_row):
        assert isinstance(row["elapsed_secs"], (int, float))
        assert row["elapsed_secs"] >= 0
        assert isinstance(row["t_start"], (int, float))
        assert row["outcome"] in ("success", "partial_mutation", "directive_failed")

    # The push leg's own clock starts no earlier than the pre-push leg ends
    # -- they are sequential, never overlapping.
    assert push_row["t_start"] >= pre_push_row["t_start"]


def test_pipeline_span_shares_composition_id_across_a_second_invocation(tmp_path):
    """Two separate `run_commit_pipeline` calls must not share a
    `composition_id` -- each invocation gets its own, so a reader can never
    conflate two different commits' pre-push/push legs into one row pair."""
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("seed", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    for i in range(2):
        (repo / f"f{i}.md").write_text("content", encoding="utf-8")
        result = run_commit_pipeline(
            repo,
            session_id=_unique_session_id(),
            subject=f"commit {i}",
            stage_paths=[f"f{i}.md"],
            caller_paths={f"f{i}.md"},
            push_mode=PUSH_MODE_SYNC,
        )
        assert result.commit_failed is False, result.diagnostics

    rows = _composition_rows(repo)
    pre_push_ids = {r["composition_id"] for r in rows if r["name"] == _COMPOSITION_SPAN_PRE_PUSH}
    assert len(pre_push_ids) == 2
