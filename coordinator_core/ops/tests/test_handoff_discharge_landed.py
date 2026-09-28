"""
coordinator_core.ops.tests.test_handoff_discharge_landed

Tier-T tests for `handoff.discharge_landed`, exercised by calling
`_handler` directly (never resolved by op key — same rationale as
test_archive_terminal_handoffs.py: resolving by key would race any
concurrent registration-key work in a peer chunk).

Real git spawn is load-bearing (the landing-sha `git log -S` lookup and the
actual archive-and-commit mover both read real git state) — one throwaway
repo per test function.

Coverage:
  - scoped (`plan_ids` given) vs repo-scope (`plan_ids` absent, scans
    docs/plans/ for implemented/landed)
  - idempotent rerun (second call over an already-discharged baton is a
    no-op / already_done)
  - concurrent second run refused cleanly (lock contention)
  - deliverable_id multi-hit refused with candidates named
  - malformed status refused
  - spawn count flat in N (one `git log -S` per distinct plan, not per baton)
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.ops import handoff_discharge_landed as mod

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> None:
    # popup-intentional-last-resort — test-only real-git spawn, mirrors
    # test_archive_terminal_handoffs.py's identical fixture helper.
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=_GIT_ENV, timeout=15,
        stdin=subprocess.DEVNULL, **no_console_creationflags(),
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _write_plan(repo: Path, rel: str, *, status: str, deliverable_id: str = "") -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    dlv_line = f"deliverable_id: {deliverable_id}\n" if deliverable_id else ""
    p.write_text(
        f"---\nstatus: {status}\n{dlv_line}---\n\n# Plan\n",
        encoding="utf-8",
    )
    return p


def _write_baton(
    repo: Path, rel: str, *, status: str = "claimed", deployment_state: str = "in_flight",
    governing_plan: str = "", deliverable_id: str = "", shipped_in: str = "",
) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"status: {status}", f"deployment_state: {deployment_state}"]
    if governing_plan:
        lines.append(f"governing_plan: {governing_plan}")
    if deliverable_id:
        lines.append(f"deliverable_id: {deliverable_id}")
    if shipped_in:
        lines.append(f"shipped_in: {shipped_in}")
    body = "\n".join(lines)
    p.write_text(f"---\n{body}\n---\n\n# Baton\n", encoding="utf-8")
    return p


def _commit_all(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def _flip_plan_status(repo: Path, plan_rel: str, status: str) -> None:
    p = repo / plan_rel
    text = p.read_text(encoding="utf-8")
    text = re.sub(r"^status:.*$", f"status: {status}", text, count=1, flags=re.MULTILINE)
    p.write_text(text, encoding="utf-8")
    _commit_all(repo, f"plan-status-transition: -> {status}")


def test_scoped_plan_ids_discharges_matching_baton(tmp_path):
    repo = _init_repo(tmp_path)
    _write_plan(repo, "docs/plans/x.md", status="draft")
    _write_baton(repo, "state/handoffs/2026-09-28-a.md", governing_plan="docs/plans/x.md")
    _commit_all(repo, "seed plan+baton")
    _flip_plan_status(repo, "docs/plans/x.md", "implemented")

    result = mod._handler({"plan_ids": ["docs/plans/x.md"]}, repo_root=repo / ".git")

    assert result["exit_code"] == 0
    assert [d["id"] for d in result["discharged"]] == ["state/handoffs/2026-09-28-a.md"]
    assert not (repo / "state/handoffs/2026-09-28-a.md").exists()
    archived = list((repo / "archive/handoffs/2026-09").glob("*.md"))
    assert len(archived) == 1


def test_repo_scope_absent_plan_ids_scans_implemented_plans(tmp_path):
    repo = _init_repo(tmp_path)
    _write_plan(repo, "docs/plans/x.md", status="draft")
    _write_baton(repo, "state/handoffs/2026-09-28-a.md", governing_plan="docs/plans/x.md")
    _commit_all(repo, "seed plan+baton")
    _flip_plan_status(repo, "docs/plans/x.md", "implemented")

    result = mod._handler({}, repo_root=repo / ".git")

    assert [d["id"] for d in result["discharged"]] == ["state/handoffs/2026-09-28-a.md"]


def test_idempotent_rerun_is_a_noop(tmp_path):
    repo = _init_repo(tmp_path)
    _write_plan(repo, "docs/plans/x.md", status="draft")
    _write_baton(repo, "state/handoffs/2026-09-28-a.md", governing_plan="docs/plans/x.md")
    _commit_all(repo, "seed plan+baton")
    _flip_plan_status(repo, "docs/plans/x.md", "implemented")

    first = mod._handler({"plan_ids": ["docs/plans/x.md"]}, repo_root=repo / ".git")
    assert len(first["discharged"]) == 1

    second = mod._handler({"plan_ids": ["docs/plans/x.md"]}, repo_root=repo / ".git")
    assert second["discharged"] == []
    assert second["refused"] == []


def test_concurrent_second_run_refused_cleanly(tmp_path):
    repo = _init_repo(tmp_path)
    _write_plan(repo, "docs/plans/x.md", status="draft")
    _write_baton(repo, "state/handoffs/2026-09-28-a.md", governing_plan="docs/plans/x.md")
    _commit_all(repo, "seed plan+baton")
    _flip_plan_status(repo, "docs/plans/x.md", "implemented")

    common_dir = repo / ".git"
    held_lock = mod._acquire_lock(common_dir)
    assert held_lock is not None
    try:
        result = mod._handler({"plan_ids": ["docs/plans/x.md"]}, repo_root=common_dir)
        assert result.get("contended") is True
        assert result["discharged"] == []
        assert result["already_done"] == []
        assert result["refused"] == []
    finally:
        mod._release_lock(held_lock)


def test_deliverable_id_multi_hit_refused_with_candidates(tmp_path):
    repo = _init_repo(tmp_path)
    _write_plan(repo, "docs/plans/x.md", status="draft", deliverable_id="dlv-shared")
    _write_plan(repo, "docs/plans/y.md", status="draft", deliverable_id="dlv-shared")
    _write_baton(repo, "state/handoffs/2026-09-28-a.md", deliverable_id="dlv-shared")
    _commit_all(repo, "seed plans+baton")
    _flip_plan_status(repo, "docs/plans/x.md", "implemented")
    _flip_plan_status(repo, "docs/plans/y.md", "implemented")

    result = mod._handler({"plan_ids": ["docs/plans/x.md", "docs/plans/y.md"]}, repo_root=repo / ".git")

    assert result["discharged"] == []
    assert len(result["refused"]) == 1
    reason = result["refused"][0]["reason"]
    assert "multi-hit" in reason
    assert "docs/plans/x.md" in reason and "docs/plans/y.md" in reason


def test_malformed_status_refused(tmp_path):
    repo = _init_repo(tmp_path)
    _write_plan(repo, "docs/plans/x.md", status="draft")
    _write_baton(
        repo, "state/handoffs/2026-09-28-a.md",
        status="complete", governing_plan="docs/plans/x.md",
    )
    _commit_all(repo, "seed plan+baton")
    _flip_plan_status(repo, "docs/plans/x.md", "implemented")

    result = mod._handler({"plan_ids": ["docs/plans/x.md"]}, repo_root=repo / ".git")

    assert result["discharged"] == []
    assert len(result["refused"]) == 1
    assert result["refused"][0]["reason"].startswith("malformed-status")


def test_spawn_count_flat_in_n_batons(tmp_path):
    """N batons, ONE distinct plan -> exactly one `git log -S` spawn for the
    landing-sha lookup, never one per baton.
    """
    repo = _init_repo(tmp_path)
    _write_plan(repo, "docs/plans/x.md", status="draft")
    for i in range(10):
        _write_baton(
            repo, f"state/handoffs/2026-09-28-b{i}.md",
            governing_plan="docs/plans/x.md",
        )
    _commit_all(repo, "seed plan+batons")
    _flip_plan_status(repo, "docs/plans/x.md", "implemented")

    real_run = subprocess.run
    log_s_calls = []

    def _spy(argv, *a, **kw):
        if len(argv) >= 2 and argv[0] == "git" and argv[1] == "log":
            log_s_calls.append(argv)
        return real_run(argv, *a, **kw)

    with patch("subprocess.run", side_effect=_spy):
        result = mod._handler({"plan_ids": ["docs/plans/x.md"]}, repo_root=repo / ".git")

    assert len(result["discharged"]) == 10
    assert len(log_s_calls) == 1


def _discharge_n_distinct_plans(tmp_path: Path, n: int) -> int:
    """Seeds N DISTINCT landed plans, each with exactly one baton, runs the
    handler over the whole batch, and returns the number of `git log`
    spawns observed (`_plan_landing_shas`'s own site, spied via
    `subprocess.run`). Returns the spawn count, not the result, so the
    caller compares counts across batch sizes directly."""
    repo = _init_repo(tmp_path)
    plan_ids = []
    for i in range(n):
        plan_rel = f"docs/plans/x{i:03d}.md"
        _write_plan(repo, plan_rel, status="draft")
        _write_baton(
            repo, f"state/handoffs/2026-09-28-p{i:03d}.md",
            governing_plan=plan_rel,
        )
        plan_ids.append(plan_rel)
    _commit_all(repo, f"seed {n} distinct plans+batons")
    for plan_rel in plan_ids:
        _flip_plan_status(repo, plan_rel, "implemented")

    real_run = subprocess.run
    log_calls = []

    def _spy(argv, *a, **kw):
        if len(argv) >= 2 and argv[0] == "git" and argv[1] == "log":
            log_calls.append(argv)
        return real_run(argv, *a, **kw)

    with patch("subprocess.run", side_effect=_spy):
        result = mod._handler({"plan_ids": plan_ids}, repo_root=repo / ".git")

    assert len(result["discharged"]) == n
    return len(log_calls)


def test_spawn_count_flat_across_distinct_plan_counts(tmp_path):
    """Spawn-flat in PLAN COUNT, not just baton count: N=5 and N=50 DISTINCT
    landed plans (one baton each) must both cost exactly ONE `git log -G`
    spawn for the whole batch (module docstring's own negative-spec: "does
    NOT spawn a git process per baton" — this asserts it also does not
    spawn one per plan).
    """
    small = tmp_path / "small"
    large = tmp_path / "large"
    small.mkdir()
    large.mkdir()

    small_calls = _discharge_n_distinct_plans(small, 5)
    large_calls = _discharge_n_distinct_plans(large, 50)

    assert small_calls == 1
    assert large_calls == 1
    assert small_calls == large_calls
