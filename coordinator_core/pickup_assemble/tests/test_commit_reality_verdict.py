"""
coordinator_core.pickup_assemble.tests.test_commit_reality_verdict — pins C2
(docs/plans/2026-08-13-legible-reconcile-surface-and-single-baton-check.md):
`brief()`'s `gates.commit_reality` verdict for the single baton under pickup,
computed via `coordinator_core.reconcile.commit_reality.evaluate_commit_reality`
mirroring `handoff_reconcile._handler`'s own call construction (DR-300 route
(d); mechanism proven standalone by
docs/research/spike-verdicts/2026-08-13-evaluate-commit-reality-standalone-single-baton.md).

Spec backlink: pln-legible-reconcile-surface-and-33eb9a, C2
Decision record: docs/decisions/DR-300-pickup-may-not-call-the-reconcile-orchestrator.md

AC4's own bar: a test that still passes with `other_open_handoffs` passed
empty has not tested the cross-handoff attribution guard. The second test
below constructs the exact flip the spike measured (2 of 6 candidate-
producing batons went `surface/partial` -> `auto-ship/high` when the guard
was disabled by an empty sequence) and asserts the GUARDED verdict — a
regression that silently swaps in `[]` for `other_open_handoffs` turns this
test red because the picked-up baton would wrongly read `auto-ship`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.pickup_assemble as pa

# Real-git spawn (init/add/commit/log) is load-bearing — `evaluate_commit_reality`
# reads actual git-tracked repo state. Declares the spawn per the ratchet's
# Rule 2(b) marker escape (coordinator_core/tests/test_no_new_spawning_tests.py)
# rather than an allowlist entry — mirrors test_read_only_invariant.py.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _isolated_git_env(anchor: Path) -> dict[str, str]:
    env = dict(os.environ)
    config_file = anchor / ".gitconfig-empty"
    if not config_file.exists():
        config_file.write_text("", encoding="utf-8")
    env["GIT_CONFIG_GLOBAL"] = str(config_file)
    env["GIT_CONFIG_SYSTEM"] = str(config_file)
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=_isolated_git_env(repo.parent),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _write_handoff(repo: Path, name: str, *, scope: list[str], title: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    scope_yaml = "\n".join(f'  - "{s}"' for s in scope)
    fm = (
        f'title: "{title}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: active\n"
        "scope:\n"
        f"{scope_yaml}\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return path


class TestBriefSurfacesCommitRealityVerdict:
    def test_verdict_reaches_the_brief_for_the_baton_under_pickup(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_handoff(repo, "h1.md", scope=["widget_polish.py"], title="No matching commit yet")
        _git(repo, "add", "state/handoffs/h1.md")
        _git(repo, "commit", "-m", "add h1")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        commit_reality = result.decision_object["gates"]["commit_reality"]
        assert set(commit_reality.keys()) >= {
            "handoff_id", "candidate_sha", "confidence", "evidence", "verdict",
        }
        # No matching commit was ever made — the read-only, no-match floor.
        assert commit_reality["verdict"] == "no-match"

    def test_other_open_handoffs_guard_is_genuinely_populated(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        # Baton under pickup: scope + title vocabulary the shipping commit's
        # subject will match on (>=2 noun tokens: "widget", "polish").
        _write_handoff(
            repo, "h1.md", scope=["widget_polish.py"], title="Polish the widget module",
        )
        # A second, UNRELATED open handoff sharing the exact same file-level
        # scope pathspec — the genuine C6/AC4 cross-handoff attribution
        # ambiguity this guard exists to catch.
        _write_handoff(
            repo, "h2.md", scope=["widget_polish.py"], title="Unrelated widget work",
        )
        _git(repo, "add", "state/handoffs/h1.md", "state/handoffs/h2.md")
        _git(repo, "commit", "-m", "add h1 and h2")

        (repo / "widget_polish.py").write_text("# widget\n", encoding="utf-8")
        _git(repo, "add", "widget_polish.py")
        _git(repo, "commit", "-m", "polish widget module cleanup")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        commit_reality = result.decision_object["gates"]["commit_reality"]
        # GUARDED verdict: h2's live, scope-overlapping presence in the open
        # set demotes this to `surface` — an empty `other_open_handoffs`
        # would have produced `auto-ship`/`high` instead (the exact flip
        # the spike measured on 2 of 6 candidate-producing batons). A
        # regression that passes `[]` here turns this assertion red.
        assert commit_reality["verdict"] == "surface"
        assert commit_reality["confidence"] == "partial"
        assert any("ambiguous attribution" in e for e in commit_reality["evidence"])


class TestComputeCommitRealitySelfExclusion:
    def test_self_excluded_others_included_from_other_open_handoffs(self, tmp_path, monkeypatch):
        # Review: coordinator:code-reviewer — isolates "self is excluded"
        # from "unrelated other is included" directly on the `other_open`
        # argument `compute_commit_reality` passes to
        # `evaluate_commit_reality`, rather than inferring it transitively
        # through the end-to-end verdict (which would still pass if
        # self-exclusion silently failed and left a spurious extra entry).
        repo = tmp_path / "repo"
        _init_repo(repo)

        _write_handoff(
            repo, "h1.md", scope=["widget_polish.py"], title="Polish the widget module",
        )
        _write_handoff(
            repo, "h2.md", scope=["other_a.py"], title="Unrelated handoff A",
        )
        _write_handoff(
            repo, "h3.md", scope=["other_b.py"], title="Unrelated handoff B",
        )
        _git(repo, "add", "state/handoffs/h1.md", "state/handoffs/h2.md", "state/handoffs/h3.md")
        _git(repo, "commit", "-m", "add h1, h2, h3")

        captured: dict[str, list] = {}

        def _capture(this_handoff, root, policy, other_open, *args, **kwargs):
            captured["other_open"] = other_open
            return {
                "handoff_id": this_handoff.get("id") or this_handoff.get("title") or "",
                "candidate_sha": None,
                "confidence": "none",
                "evidence": [],
                "verdict": "no-match",
            }

        monkeypatch.setattr(pa, "evaluate_commit_reality", _capture)

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert "other_open" in captured
        captured_paths = {
            Path(h.get("_path") or "").name for h in captured["other_open"]
        }
        assert "h1.md" not in captured_paths
        assert captured_paths == {"h2.md", "h3.md"}


class TestBriefDegradesCommitRealityRatherThanFailing:
    def test_underlying_raise_surfaces_as_unavailable_not_a_brief_failure(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_handoff(repo, "h1.md", scope=["widget_polish.py"], title="No matching commit yet")
        _git(repo, "add", "state/handoffs/h1.md")
        _git(repo, "commit", "-m", "add h1")

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated git failure")

        monkeypatch.setattr(pa, "evaluate_commit_reality", _boom)

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        commit_reality = result.decision_object["gates"]["commit_reality"]
        assert commit_reality["verdict"] == "unavailable"
        assert any("simulated git failure" in e for e in commit_reality["evidence"])
