"""
coordinator_core.ops.tests.test_cascade_backstop_sweep — pytest for the
"deliverable.cascade_backstop_sweep" op (C6c: docs/plans/2026-08-04-terminal-state-propagation-join-keys.md, AC6d).

Run (from repo root): python3 -m pytest coordinator_core/ops/tests/test_cascade_backstop_sweep.py -q
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

import coordinator_core.ops.cascade_backstop_sweep as sweep_mod
import coordinator_core.ops.handoff_children  # noqa: F401 — fires @register_op side effect
import coordinator_core.ops.handoff_transition  # noqa: F401 — fires @register_op side effect
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter

_handler = sweep_mod._handler

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _seed_handoff(
    repo: Path,
    name: str,
    *,
    deployment_state: str = "ready_to_fire",
    deliverable_id: str = "dlv-test-000000",
    archived: bool = False,
    extra: str = "",
) -> Path:
    subdir = "archive/handoffs" if archived else "state/handoffs"
    path = repo / subdir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        f"deployment_state: {deployment_state}\n"
        f"deliverable_id: {deliverable_id}\n"
    )
    if extra:
        fm += extra
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return path


def _seed_plan(repo: Path, name: str, *, status: str = "implemented", deliverable_id: str = "dlv-test-000000") -> Path:
    path = repo / "docs" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\ntitle: "Test Plan"\nstatus: {status}\ndeliverable_id: "{deliverable_id}"\n---\n\n# Plan\n',
        encoding="utf-8",
    )
    return path


def _run(params: dict, repo_root: Path) -> dict:
    return asyncio.run(_handler(params, repo_root=repo_root))


def _fm_field(path: Path, key: str) -> Optional[str]:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None
    return read_fm_field(split.fm_text, key)


class TestNeverWrites:
    """The load-bearing negative assertion — see plan Anti-scope bullet 2 and
    module NEGATIVE-SPEC: this sweep reports, it never flips."""

    def test_a_clearing_candidate_is_reported_but_left_untouched_on_disk(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_plan(repo, "p1.md", status="implemented", deliverable_id="dlv-abc-111111")
        handoff = _seed_handoff(repo, "h1.md", deliverable_id="dlv-abc-111111")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "seed fixture")

        before_bytes = handoff.read_bytes()
        before_mtime = handoff.stat().st_mtime

        result = _run({}, repo_root=repo / ".git")

        assert result["exit_code"] == 0
        divergent_paths = {d["handoff_path"] for d in result["divergences"]}
        assert str(handoff) in divergent_paths

        after_bytes = handoff.read_bytes()
        after_mtime = handoff.stat().st_mtime
        assert after_bytes == before_bytes, "backstop sweep must never mutate a candidate's bytes"
        assert after_mtime == before_mtime, "backstop sweep must never touch a candidate's file"
        assert _fm_field(handoff, "deployment_state") == "ready_to_fire"
        assert _fm_field(handoff, "advanced_by") is None

        # No git-visible dirty tree either — this op takes no path that writes.
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
        )
        assert status.stdout.strip() == "", f"backstop sweep left a dirty tree: {status.stdout!r}"


class TestDivergenceDetection:
    def test_reports_divergence_from_a_terminal_plan_source(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_plan(repo, "p1.md", status="implemented", deliverable_id="dlv-abc-111111")
        _seed_handoff(repo, "h1.md", deliverable_id="dlv-abc-111111", deployment_state="ready_to_fire")

        result = _run({}, repo_root=repo / ".git")
        assert len(result["divergences"]) == 1
        assert result["divergences"][0]["deliverable_id"] == "dlv-abc-111111"
        assert "docs/plans/p1.md" in result["divergences"][0]["terminal_sources"]

    def test_reports_divergence_from_a_terminal_archived_handoff_source(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(
            repo, "shipped.md", deliverable_id="dlv-cockpit-000001",
            deployment_state="shipped", archived=True,
        )
        _seed_handoff(
            repo, "live.md", deliverable_id="dlv-cockpit-000001",
            deployment_state="ready_to_fire",
        )

        result = _run({}, repo_root=repo / ".git")
        assert len(result["divergences"]) == 1
        assert result["divergences"][0]["deliverable_id"] == "dlv-cockpit-000001"

    def test_a_refused_candidate_is_not_reported_as_a_divergence(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_plan(repo, "p1.md", status="implemented", deliverable_id="dlv-abc-222222")
        _seed_handoff(
            repo, "blocked.md", deliverable_id="dlv-abc-222222",
            deployment_state="awaiting_gate",
        )

        result = _run({}, repo_root=repo / ".git")
        assert result["divergences"] == []
        assert result["deliverable_ids_checked"] == 1

    def test_no_terminal_sources_is_a_clean_report_not_an_error(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md", deliverable_id="dlv-nobody-000000")

        result = _run({}, repo_root=repo / ".git")
        assert result["exit_code"] == 0
        assert result["divergences"] == []
        assert result["deliverable_ids_checked"] == 0

    def test_deliverable_id_param_restricts_the_scan(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_plan(repo, "p1.md", status="implemented", deliverable_id="dlv-one-111111")
        _seed_plan(repo, "p2.md", status="implemented", deliverable_id="dlv-two-222222")
        _seed_handoff(repo, "h1.md", deliverable_id="dlv-one-111111")
        _seed_handoff(repo, "h2.md", deliverable_id="dlv-two-222222")

        result = _run({"deliverable_id": "dlv-one-111111"}, repo_root=repo / ".git")
        assert result["deliverable_ids_checked"] == 1
        assert {d["deliverable_id"] for d in result["divergences"]} == {"dlv-one-111111"}

    def test_missing_repo_root_is_an_error(self):
        result = asyncio.run(_handler({}, repo_root=None))
        assert result["exit_code"] == 1
        assert "repo_root" in result["error"]
