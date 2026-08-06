"""
coordinator_core.tests.test_dag_resolve_target_tier3_dedup — Regression coverage for the
resolve_target() tier-3 dedup pass (2026-07-23 follow-up to the boot_sweep 10s-timeout
memoization fix in 2159bc1c).

resolve_target()'s tier-3 block queries `target` itself, then re-derives each entry of
candidates[] as repo-relative and queries those too. When `target` is already a
repo-relative archive path, several re-derived candidates collapse to the SAME
repo-relative string already asked (or to a structurally-doubled prefix that can never
match). This module verifies:
  (1) a repo-relative archive ref resolves identically before and after the dedup change
      (the anti-regression case);
  (2) the tier-3 block issues no duplicate ever_tracked/_git_path_ever_tracked lookups
      for a single resolve_target() call;
  (3) a ref that genuinely only resolves via a re-derived candidate (not the raw target)
      still resolves — guards against over-trimming.

Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-boot-sweep-10s-timeout.md
(follow-up tidiness pass, not the original perf fix)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import dag


@pytest.fixture(autouse=True)
def clear_ever_tracked_cache():
    dag._EVER_TRACKED_CACHE.clear()
    yield
    dag._EVER_TRACKED_CACHE.clear()


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    return root


def _commit_file(root: Path, rel_path: str, content: str = "x") -> None:
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    subprocess.run(["git", "add", "--", rel_path], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", f"add {rel_path}"],
        cwd=root, check=True,
    )


# ---------------------------------------------------------------------------
# (1) Anti-regression: a repo-relative archive ref, deleted from disk but
#     present in git history, resolves to 'git-history' identically.
# ---------------------------------------------------------------------------

class TestSameResultBeforeAndAfterDedup:
    def test_repo_relative_archive_ref_resolves_via_git_history(self, tmp_path):
        root = _init_repo(tmp_path)
        rel_path = "archive/handoffs/2026-07/foo.md"
        _commit_file(root, rel_path)
        # Remove from disk so only tier 3 (git history) can resolve it.
        (root / rel_path).unlink()
        subprocess.run(["git", "add", "--", rel_path], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "remove foo.md"],
            cwd=root, check=True,
        )

        handoff_dir = str(root / "state" / "handoffs")
        result = dag.resolve_target(rel_path, handoff_dir, str(root))

        assert result == "git-history"


# ---------------------------------------------------------------------------
# (2) No duplicate ever_tracked lookups within a single resolve_target() call.
# ---------------------------------------------------------------------------

class TestNoDuplicateLookups:
    def test_tier3_issues_no_duplicate_git_path_ever_tracked_calls(self, tmp_path):
        root = _init_repo(tmp_path)
        _commit_file(root, "state/handoffs/seed.md")

        calls = []
        orig = dag._git_path_ever_tracked

        def counting(repo_rel_path, repo_root):
            calls.append(repo_rel_path)
            return orig(repo_rel_path, repo_root)

        dag._git_path_ever_tracked = counting
        try:
            handoff_dir = str(root / "state" / "handoffs")
            # A never-existed archive-relative ref forces every tier-3 candidate
            # to be exhausted (all return False), maximizing lookup surface.
            dag.resolve_target(
                "archive/handoffs/2026-07/never-existed.md", handoff_dir, str(root)
            )
        finally:
            dag._git_path_ever_tracked = orig

        assert len(calls) == len(set(calls)), (
            f"expected no duplicate ever_tracked lookups within one resolve_target() "
            f"call, got calls={calls}"
        )


# ---------------------------------------------------------------------------
# (4) Memoization correctness, tested directly at the _memoized_ever_tracked
#     unit: a repeat ask for a key that resolved True must return the stored
#     True, not a stale/default False — regardless of how many times, or in
#     what order, it's asked. This is deliberately NOT routed through
#     resolve_target(), because every current resolve_target() call site
#     returns immediately on a True result — a "seen set, return False on
#     repeat" guard and a "memoize the real result" guard are behaviourally
#     indistinguishable through resolve_target()'s existing call sites today.
#     Testing the helper directly is what makes correctness independent of
#     that call-site structure, present or future.
# ---------------------------------------------------------------------------

class TestMemoReturnsStoredTrueNotStaleFalse:
    def test_repeat_ask_for_tracked_key_returns_true_not_false(self, tmp_path):
        root = _init_repo(tmp_path)
        rel_path = "state/handoffs/tracked.md"
        _commit_file(root, rel_path)
        (root / rel_path).unlink()
        subprocess.run(["git", "add", "--", rel_path], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "remove tracked.md"],
            cwd=root, check=True,
        )

        memo: dict = {}
        first = dag._memoized_ever_tracked(rel_path, memo, str(root), None)
        second = dag._memoized_ever_tracked(rel_path, memo, str(root), None)

        assert first is True
        assert second is True, (
            "expected a repeat ask for a genuinely-tracked key to return the "
            "memoized True, not a stale/default False"
        )

    def test_repeat_ask_spawns_git_only_once(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        rel_path = "state/handoffs/tracked.md"
        _commit_file(root, rel_path)

        calls = []
        orig = dag._git_path_ever_tracked

        def counting(p, r):
            calls.append(p)
            return orig(p, r)

        monkeypatch.setattr(dag, "_git_path_ever_tracked", counting)

        memo: dict = {}
        for _ in range(5):
            assert dag._memoized_ever_tracked(rel_path, memo, str(root), None) is True

        assert len(calls) == 1, (
            f"expected exactly one _git_path_ever_tracked spawn across 5 repeat "
            f"asks of the same key, got {len(calls)}"
        )


# ---------------------------------------------------------------------------
# (3) Over-trimming guard: a ref that only resolves via a re-derived
#     candidate (not the raw target string) must still resolve.
# ---------------------------------------------------------------------------

class TestReDerivedCandidateStillResolves:
    def test_bare_basename_ref_resolves_via_rederived_archive_candidate(self, tmp_path):
        root = _init_repo(tmp_path)
        # Tracked only under archive/handoffs/<basename>, not under the bare
        # basename itself — resolvable only by re-deriving candidates[] (the
        # `archive/handoffs/<basename>` candidate) as repo-relative.
        rel_path = "archive/handoffs/bar.md"
        _commit_file(root, rel_path)
        (root / rel_path).unlink()
        subprocess.run(["git", "add", "--", rel_path], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "remove bar.md"],
            cwd=root, check=True,
        )

        handoff_dir = str(root / "state" / "handoffs")
        result = dag.resolve_target("bar.md", handoff_dir, str(root))

        assert result == "git-history"
