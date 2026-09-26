"""
coordinator_core.ops.tests.test_completion_nature

Tests for the heuristic completion-nature classifier.

Coverage:
  (a) roadmap     — plan-doc paths + roadmap/feature commit keywords
  (b) bugfix      — bug-backlog paths + fix/revert/regression keywords
  (c) tech-debt   — improvement-queue paths + refactor/cleanup keywords
  (d) infra       — CI/scripts paths + chore/install/config keywords
  (e) env-override — COMPLETION_NATURE set → returned verbatim, heuristics bypassed
  (f) priority    — bugfix wins over roadmap when both have signals
  (g) empty-input — no paths, no commits → default "infra"
  (h) roadmap-wins-over-tech-debt — plan paths + consolidat keyword
  (i) commit-keyword-alone — paths empty, commit message drives nature
  (j) path-alone  — commits empty, touched path drives nature

Spec backlink: coordinator_core/ops/completion_nature.py
Node-map: wsc step 2.6.4
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.ops.completion_nature import classify_nature


def _run(paths, commits, *, env_override: str | None = None) -> str:
    """Call classify_nature, optionally injecting COMPLETION_NATURE env var.

    Restores env state after the call regardless of outcome.
    """
    key = "COMPLETION_NATURE"
    old = os.environ.get(key)
    try:
        if env_override is not None:
            os.environ[key] = env_override
        elif key in os.environ:
            del os.environ[key]
        return classify_nature(paths, commits)
    finally:
        if old is not None:
            os.environ[key] = old
        elif key in os.environ:
            del os.environ[key]


def test_roadmap_plan_doc_paths():
    paths = [
        "docs/plans/2026-07-06-new-pipeline-design.md",
        "docs/problems/2026-07-06-ceremony-inversion.md",
    ]
    assert _run(paths, []) == "roadmap"


def test_roadmap_commit_keywords():
    commits = [
        "feat(ceremony): implement wsc_resolve pre-resolver",
        "add receipt schema + graceful-absent emit",
    ]
    assert _run([], commits) == "roadmap"


def test_roadmap_combined():
    paths = ["docs/plans/2026-07-06-invert-wsc.md"]
    commits = ["plan(wsc-inversion): draft ceremony pipeline design"]
    assert _run(paths, commits) == "roadmap"


def test_roadmap_ceremony_op_path():
    paths = [
        "coordinator_core/ops/ceremony/wsc_resolve.py",
        "coordinator_core/ops/ceremony/receipt_schema.py",
    ]
    commits = ["feat: add wsc_resolve pre-resolver"]
    assert _run(paths, commits) == "roadmap"


def test_bugfix_path():
    paths = ["state/bug-backlog/2026-07-05-emit-regression.yaml"]
    assert _run(paths, []) == "bugfix"


def test_bugfix_fix_keyword():
    commits = ["fix(roadmap-dag): correct emit ordering for chain terminal"]
    assert _run([], commits) == "bugfix"


def test_bugfix_revert_keyword():
    commits = ["revert: undo accidental session-shape.json schema change"]
    assert _run([], commits) == "bugfix"


def test_bugfix_regression_keyword():
    commits = ["test: reproduce coverage-gate regression (#217)"]
    assert _run([], commits) == "bugfix"


def test_bugfix_hotfix_keyword():
    commits = ["hotfix: guard absent session-shape against KeyError"]
    assert _run([], commits) == "bugfix"


def test_bugfix_combined():
    paths = ["state/bug-backlog/2026-07-06-stale-receipt.yaml"]
    commits = ["fix(receipt): emit op_tail even when tail not run"]
    assert _run(paths, commits) == "bugfix"


def test_tech_debt_improvement_queue_path():
    paths = ["state/improvement-queue/2026-07-04-remove-legacy-bash.yaml"]
    assert _run(paths, []) == "tech-debt"


def test_tech_debt_refactor_keyword():
    commits = ["refactor(ops): extract common worktree derivation to helper"]
    assert _run([], commits) == "tech-debt"


def test_tech_debt_cleanup_keyword():
    commits = ["cleanup: remove deprecated coordinator-session.sh legacy paths"]
    assert _run([], commits) == "tech-debt"


def test_tech_debt_consolidate_keyword():
    commits = ["consolidate fleet archive ops into single helper"]
    assert _run([], commits) == "tech-debt"


def test_tech_debt_strangle_keyword():
    commits = ["strangle: migrate coordinator-session.sh cs_archive to Python"]
    assert _run([], commits) == "tech-debt"


def test_infra_scripts_path():
    paths = ["scripts/setup.sh"]
    assert _run(paths, []) == "infra"


def test_infra_bin_path():
    paths = ["bin/claude-klabauter-doctor-probe.py"]
    assert _run(paths, []) == "infra"


def test_infra_chore_keyword():
    commits = ["chore: bump coordinator_core to v2.6.0"]
    assert _run([], commits) == "infra"


def test_infra_install_keyword():
    commits = ["install: update agent-install-manifest.json repo_id"]
    assert _run([], commits) == "infra"


def test_infra_vendor_keyword():
    commits = ["vendor(cockpit-contract): re-vendor v2.6.0 + reader-first ancestry"]
    assert _run([], commits) == "infra"


def test_infra_requirements_path():
    paths = ["requirements-dev.txt"]
    assert _run(paths, []) == "infra"


def test_infra_pyproject_path():
    paths = ["pyproject.toml"]
    assert _run(paths, []) == "infra"


def test_infra_github_path():
    paths = [".github/workflows/ci.yml"]
    assert _run(paths, []) == "infra"


def test_infra_skills_setup_path():
    paths = ["skills/setup/SKILL.md"]
    assert _run(paths, []) == "infra"


# (e) env override — COMPLETION_NATURE bypasses all heuristics


def test_env_override_returns_verbatim():
    """COMPLETION_NATURE env var → returned verbatim regardless of signals."""
    paths = ["state/bug-backlog/crash.yaml"]
    commits = ["fix: urgent crash fix"]
    result = _run(paths, commits, env_override="roadmap")
    assert result == "roadmap"


def test_env_override_arbitrary_value():
    """COMPLETION_NATURE can hold any string — returned verbatim."""
    result = _run([], [], env_override="custom-nature-value")
    assert result == "custom-nature-value"


def test_env_override_empty_not_active():
    """Empty COMPLETION_NATURE string is ignored — heuristic runs normally."""
    result = _run([], ["chore: bump"], env_override="")
    assert result == "infra"


def test_env_override_bugfix_override():
    """COMPLETION_NATURE=bugfix overrides even strong infra signals."""
    paths = ["scripts/setup.sh", ".github/workflows/ci.yml"]
    commits = ["chore: update CI pipeline"]
    result = _run(paths, commits, env_override="bugfix")
    assert result == "bugfix"


def test_priority_bugfix_over_roadmap():
    commits = [
        "fix: correct roadmap DAG emit ordering",
        "feat: add new emission path",
    ]
    assert _run([], commits) == "bugfix"


def test_priority_bugfix_over_tech_debt():
    commits = [
        "fix(cleanup): remove stale refactor path",
    ]
    result = _run([], commits)
    assert result == "bugfix"


def test_priority_roadmap_over_tech_debt():
    paths = [
        "docs/plans/2026-07-06-new-feature.md",
        "state/improvement-queue/2026-07-06.yaml",
    ]
    result = _run(paths, [])
    assert result == "roadmap"


def test_priority_roadmap_over_infra():
    paths = [
        "docs/plans/2026-07-06-new-feature.md",
        "scripts/setup.sh",
    ]
    result = _run(paths, [])
    assert result == "roadmap"


def test_empty_input_returns_infra():
    assert _run([], []) == "infra"


def test_no_signal_paths_only():
    paths = ["src/some_module.py", "README.md"]
    assert _run(paths, []) == "infra"


def test_no_signal_commits_only():
    commits = ["update some stuff", "tweak parameter"]
    result = _run([], commits)
    assert result == "infra"


def test_roadmap_wins_with_higher_score():
    paths = [
        "docs/plans/2026-07-06-plan-a.md",
        "docs/problems/2026-07-06-prob.md",
    ]
    commits = [
        "consolidate old behavior",
    ]
    result = _run(paths, commits)
    assert result == "roadmap"


def test_commit_alone_drives_roadmap():
    assert _run([], ["feat: implement wsc resolver"]) == "roadmap"


def test_commit_alone_drives_bugfix():
    assert _run([], ["fix: guard absent session-shape"]) == "bugfix"


def test_commit_alone_drives_tech_debt():
    assert _run([], ["refactor: extract worktree helper"]) == "tech-debt"


def test_commit_alone_drives_infra():
    assert _run([], ["chore: update pyproject.toml"]) == "infra"


def test_path_alone_drives_roadmap():
    assert _run(["docs/plans/2026-07-06-something.md"], []) == "roadmap"


def test_path_alone_drives_bugfix():
    assert _run(["state/bug-backlog/2026-07-06-issue.yaml"], []) == "bugfix"


def test_path_alone_drives_tech_debt():
    assert _run(["state/improvement-queue/2026-07-06-dedup.yaml"], []) == "tech-debt"


def test_path_alone_drives_infra():
    assert _run(["scripts/setup.sh"], []) == "infra"
