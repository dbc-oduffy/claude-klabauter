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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
            # Ensure the env is clean for non-override tests.
            del os.environ[key]
        return classify_nature(paths, commits)
    finally:
        if old is not None:
            os.environ[key] = old
        elif key in os.environ:
            del os.environ[key]


# ---------------------------------------------------------------------------
# (a) roadmap
# ---------------------------------------------------------------------------


def test_roadmap_plan_doc_paths():
    """Plan-doc paths alone → roadmap."""
    paths = [
        "docs/plans/2026-07-06-new-pipeline-design.md",
        "docs/problems/2026-07-06-ceremony-inversion.md",
    ]
    assert _run(paths, []) == "roadmap"


def test_roadmap_commit_keywords():
    """Feature/implement commit keywords → roadmap."""
    commits = [
        "feat(ceremony): implement wsc_resolve pre-resolver",
        "add receipt schema + graceful-absent emit",
    ]
    assert _run([], commits) == "roadmap"


def test_roadmap_combined():
    """Plan paths + roadmap commits → roadmap."""
    paths = ["docs/plans/2026-07-06-invert-wsc.md"]
    commits = ["plan(wsc-inversion): draft ceremony pipeline design"]
    assert _run(paths, commits) == "roadmap"


def test_roadmap_ceremony_op_path():
    """ceremony op paths → roadmap (ceremony work is roadmap-class)."""
    paths = [
        "coordinator_core/ops/ceremony/wsc_resolve.py",
        "coordinator_core/ops/ceremony/receipt_schema.py",
    ]
    commits = ["feat: add wsc_resolve pre-resolver"]
    assert _run(paths, commits) == "roadmap"


# ---------------------------------------------------------------------------
# (b) bugfix
# ---------------------------------------------------------------------------


def test_bugfix_path():
    """bug-backlog path alone → bugfix."""
    paths = ["state/bug-backlog/2026-07-05-emit-regression.yaml"]
    assert _run(paths, []) == "bugfix"


def test_bugfix_fix_keyword():
    """'fix' commit keyword → bugfix."""
    commits = ["fix(roadmap-dag): correct emit ordering for chain terminal"]
    assert _run([], commits) == "bugfix"


def test_bugfix_revert_keyword():
    """'revert' commit keyword → bugfix."""
    commits = ["revert: undo accidental session-shape.json schema change"]
    assert _run([], commits) == "bugfix"


def test_bugfix_regression_keyword():
    """'regression' in commit message → bugfix."""
    commits = ["test: reproduce coverage-gate regression (#217)"]
    assert _run([], commits) == "bugfix"


def test_bugfix_hotfix_keyword():
    """'hotfix' compound keyword → bugfix."""
    commits = ["hotfix: guard absent session-shape against KeyError"]
    assert _run([], commits) == "bugfix"


def test_bugfix_combined():
    """bug-backlog path + fix keyword → bugfix."""
    paths = ["state/bug-backlog/2026-07-06-stale-receipt.yaml"]
    commits = ["fix(receipt): emit op_tail even when tail not run"]
    assert _run(paths, commits) == "bugfix"


# ---------------------------------------------------------------------------
# (c) tech-debt
# ---------------------------------------------------------------------------


def test_tech_debt_improvement_queue_path():
    """improvement-queue path alone → tech-debt."""
    paths = ["state/improvement-queue/2026-07-04-remove-legacy-bash.yaml"]
    assert _run(paths, []) == "tech-debt"


def test_tech_debt_refactor_keyword():
    """'refactor' commit keyword → tech-debt."""
    commits = ["refactor(ops): extract common worktree derivation to helper"]
    assert _run([], commits) == "tech-debt"


def test_tech_debt_cleanup_keyword():
    """'cleanup' keyword → tech-debt."""
    commits = ["cleanup: remove deprecated coordinator-session.sh legacy paths"]
    assert _run([], commits) == "tech-debt"


def test_tech_debt_consolidate_keyword():
    """'consolidat' keyword → tech-debt."""
    commits = ["consolidate fleet archive ops into single helper"]
    assert _run([], commits) == "tech-debt"


def test_tech_debt_strangle_keyword():
    """'strangl' keyword (strangler pattern) → tech-debt."""
    commits = ["strangle: migrate coordinator-session.sh cs_archive to Python"]
    assert _run([], commits) == "tech-debt"


# ---------------------------------------------------------------------------
# (d) infra
# ---------------------------------------------------------------------------


def test_infra_scripts_path():
    """scripts/ path alone → infra."""
    paths = ["scripts/setup.sh"]
    assert _run(paths, []) == "infra"


def test_infra_bin_path():
    """bin/ path alone → infra."""
    paths = ["bin/makima-doctor-probe.py"]
    assert _run(paths, []) == "infra"


def test_infra_chore_keyword():
    """'chore' commit keyword → infra."""
    commits = ["chore: bump coordinator_core to v2.6.0"]
    assert _run([], commits) == "infra"


def test_infra_install_keyword():
    """'install' commit keyword → infra."""
    commits = ["install: update agent-install-manifest.json repo_id"]
    assert _run([], commits) == "infra"


def test_infra_vendor_keyword():
    """'vendor' commit keyword → infra (e.g. re-vendor cockpit-contract)."""
    commits = ["vendor(cockpit-contract): re-vendor v2.6.0 + reader-first ancestry"]
    assert _run([], commits) == "infra"


def test_infra_requirements_path():
    """requirements*.txt path → infra."""
    paths = ["requirements-dev.txt"]
    assert _run(paths, []) == "infra"


def test_infra_pyproject_path():
    """pyproject.toml path → infra."""
    paths = ["pyproject.toml"]
    assert _run(paths, []) == "infra"


def test_infra_github_path():
    """.github/ path → infra."""
    paths = [".github/workflows/ci.yml"]
    assert _run(paths, []) == "infra"


def test_infra_skills_setup_path():
    """skills/setup/ path → infra."""
    paths = ["skills/setup/SKILL.md"]
    assert _run(paths, []) == "infra"


# ---------------------------------------------------------------------------
# (e) env override — COMPLETION_NATURE bypasses all heuristics
# ---------------------------------------------------------------------------


def test_env_override_returns_verbatim():
    """COMPLETION_NATURE env var → returned verbatim regardless of signals."""
    paths = ["state/bug-backlog/crash.yaml"]
    commits = ["fix: urgent crash fix"]
    # Even strong bugfix signals are bypassed when the env override is set.
    result = _run(paths, commits, env_override="roadmap")
    assert result == "roadmap"


def test_env_override_arbitrary_value():
    """COMPLETION_NATURE can hold any string — returned verbatim."""
    result = _run([], [], env_override="custom-nature-value")
    assert result == "custom-nature-value"


def test_env_override_empty_not_active():
    """Empty COMPLETION_NATURE string is ignored — heuristic runs normally."""
    # chore keyword → infra
    result = _run([], ["chore: bump"], env_override="")
    assert result == "infra"


def test_env_override_bugfix_override():
    """COMPLETION_NATURE=bugfix overrides even strong infra signals."""
    paths = ["scripts/setup.sh", ".github/workflows/ci.yml"]
    commits = ["chore: update CI pipeline"]
    result = _run(paths, commits, env_override="bugfix")
    assert result == "bugfix"


# ---------------------------------------------------------------------------
# (f) priority — bugfix > roadmap when both have signals
# ---------------------------------------------------------------------------


def test_priority_bugfix_over_roadmap():
    """When both bugfix and roadmap keywords are present, bugfix wins."""
    commits = [
        "fix: correct roadmap DAG emit ordering",  # bugfix keyword
        "feat: add new emission path",              # roadmap keyword
    ]
    # bugfix has priority over roadmap in the heuristic ordering.
    assert _run([], commits) == "bugfix"


def test_priority_bugfix_over_tech_debt():
    """bugfix wins over tech-debt when both fire."""
    commits = [
        "fix(cleanup): remove stale refactor path",  # fix = bugfix; cleanup = tech-debt
    ]
    # "fix" fires bugfix; "cleanup" fires tech-debt. bugfix has higher priority.
    result = _run([], commits)
    assert result == "bugfix"


def test_priority_roadmap_over_tech_debt():
    """roadmap wins over tech-debt when both fire without bugfix."""
    paths = [
        "docs/plans/2026-07-06-new-feature.md",      # roadmap
        "state/improvement-queue/2026-07-06.yaml",   # tech-debt
    ]
    # Equal path hits; roadmap has higher priority.
    result = _run(paths, [])
    # Both score 1; roadmap wins by priority.
    assert result == "roadmap"


def test_priority_roadmap_over_infra():
    """roadmap wins over infra when both fire without stronger signals."""
    paths = [
        "docs/plans/2026-07-06-new-feature.md",  # roadmap
        "scripts/setup.sh",                       # infra
    ]
    result = _run(paths, [])
    assert result == "roadmap"


# ---------------------------------------------------------------------------
# (g) empty input — default "infra"
# ---------------------------------------------------------------------------


def test_empty_input_returns_infra():
    """No paths, no commits → default 'infra'."""
    assert _run([], []) == "infra"


def test_no_signal_paths_only():
    """Paths that match no pattern → default 'infra'."""
    paths = ["src/some_module.py", "README.md"]
    assert _run(paths, []) == "infra"


def test_no_signal_commits_only():
    """Commit messages that match no keyword → default 'infra'."""
    commits = ["update some stuff", "tweak parameter"]
    # "update" and "tweak" are not in any keyword list; default to infra.
    result = _run([], commits)
    assert result == "infra"


# ---------------------------------------------------------------------------
# (h) roadmap wins over tech-debt with mixed signals
# ---------------------------------------------------------------------------


def test_roadmap_wins_with_higher_score():
    """When roadmap accumulates more hits than tech-debt, roadmap wins."""
    paths = [
        "docs/plans/2026-07-06-plan-a.md",   # roadmap hit
        "docs/problems/2026-07-06-prob.md",  # roadmap hit
    ]
    commits = [
        "consolidate old behavior",   # tech-debt hit
    ]
    # roadmap: 2 path hits; tech-debt: 1 commit hit → roadmap wins
    result = _run(paths, commits)
    assert result == "roadmap"


# ---------------------------------------------------------------------------
# (i) commit-keyword alone drives nature
# ---------------------------------------------------------------------------


def test_commit_alone_drives_roadmap():
    """Empty paths; roadmap commit → roadmap."""
    assert _run([], ["feat: implement wsc resolver"]) == "roadmap"


def test_commit_alone_drives_bugfix():
    """Empty paths; fix commit → bugfix."""
    assert _run([], ["fix: guard absent session-shape"]) == "bugfix"


def test_commit_alone_drives_tech_debt():
    """Empty paths; refactor commit → tech-debt."""
    assert _run([], ["refactor: extract worktree helper"]) == "tech-debt"


def test_commit_alone_drives_infra():
    """Empty paths; chore commit → infra."""
    assert _run([], ["chore: update pyproject.toml"]) == "infra"


# ---------------------------------------------------------------------------
# (j) path alone drives nature
# ---------------------------------------------------------------------------


def test_path_alone_drives_roadmap():
    """No commits; plan doc path → roadmap."""
    assert _run(["docs/plans/2026-07-06-something.md"], []) == "roadmap"


def test_path_alone_drives_bugfix():
    """No commits; bug-backlog path → bugfix."""
    assert _run(["state/bug-backlog/2026-07-06-issue.yaml"], []) == "bugfix"


def test_path_alone_drives_tech_debt():
    """No commits; improvement-queue path → tech-debt."""
    assert _run(["state/improvement-queue/2026-07-06-dedup.yaml"], []) == "tech-debt"


def test_path_alone_drives_infra():
    """No commits; scripts path → infra."""
    assert _run(["scripts/setup.sh"], []) == "infra"
