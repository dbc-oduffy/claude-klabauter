
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

from coordinator_core import dag
from coordinator_core.win_portability import no_console_passthrough_kwargs


@pytest.fixture(autouse=True)
def clear_ever_tracked_cache():
    dag._EVER_TRACKED_CACHE.clear()
    yield
    dag._EVER_TRACKED_CACHE.clear()


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True, **no_console_passthrough_kwargs())
    return root


def _commit_file(root: Path, rel_path: str, content: str = "x") -> None:
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    subprocess.run(["git", "add", "--", rel_path], cwd=root, check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", f"add {rel_path}"],
        cwd=root, check=True,
        **no_console_passthrough_kwargs(),
    )


class TestSameResultBeforeAndAfterDedup:
    def test_repo_relative_archive_ref_resolves_via_git_history(self, tmp_path):
        root = _init_repo(tmp_path)
        rel_path = "archive/handoffs/2026-07/foo.md"
        _commit_file(root, rel_path)
        (root / rel_path).unlink()
        subprocess.run(["git", "add", "--", rel_path], cwd=root, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "remove foo.md"],
            cwd=root, check=True,
            **no_console_passthrough_kwargs(),
        )

        handoff_dir = str(root / "state" / "handoffs")
        result = dag.resolve_target(rel_path, handoff_dir, str(root))

        assert result == "git-history"


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
            dag.resolve_target(
                "archive/handoffs/2026-07/never-existed.md", handoff_dir, str(root)
            )
        finally:
            dag._git_path_ever_tracked = orig

        assert len(calls) == len(set(calls)), (
            f"expected no duplicate ever_tracked lookups within one resolve_target() "
            f"call, got calls={calls}"
        )


class TestMemoReturnsStoredTrueNotStaleFalse:
    def test_repeat_ask_for_tracked_key_returns_true_not_false(self, tmp_path):
        root = _init_repo(tmp_path)
        rel_path = "state/handoffs/tracked.md"
        _commit_file(root, rel_path)
        (root / rel_path).unlink()
        subprocess.run(["git", "add", "--", rel_path], cwd=root, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "remove tracked.md"],
            cwd=root, check=True,
            **no_console_passthrough_kwargs(),
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


class TestReDerivedCandidateStillResolves:
    def test_bare_basename_ref_resolves_via_rederived_archive_candidate(self, tmp_path):
        root = _init_repo(tmp_path)
        rel_path = "archive/handoffs/bar.md"
        _commit_file(root, rel_path)
        (root / rel_path).unlink()
        subprocess.run(["git", "add", "--", rel_path], cwd=root, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "remove bar.md"],
            cwd=root, check=True,
            **no_console_passthrough_kwargs(),
        )

        handoff_dir = str(root / "state" / "handoffs")
        result = dag.resolve_target("bar.md", handoff_dir, str(root))

        assert result == "git-history"
