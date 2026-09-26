"""
coordinator_core.tests.test_dag_git_path_ever_tracked_cache — Regression coverage for the
2026-07-23 boot_sweep 10s-timeout perf fix (dag._git_path_ever_tracked memoization).

Root cause (measured against a 72-handoff/497-plan/37-memo corpus): a single boot_sweep
run spawned 1053 `git log --all -- <path>` subprocesses (~14.6s total wall-clock) to
resolve only 20 UNIQUE (repo_root, repo_rel_path) values — the same ~20 questions asked
~50x each. This module verifies the process-lifetime cache added to
dag._git_path_ever_tracked collapses repeat lookups to a single subprocess spawn, that
negative (False) results are cached (the expensive majority per the measurement), and
that dag.invalidate_git_history_cache() forces a stale-negative path to re-query rather
than serve a pre-commit cached False forever.

Also covers the 2026-07-29 build_git_history_cache widening (dropping --diff-filter=A,
adding --no-renames) — regression coverage for a follow-on defect measured against
DoE-claude, where the ADD-only priming pass left handoffs.collect() spawning 314 unique
per-path `git log --all -- <path>` fallback subprocesses because it never caught a path
renamed into its final name.

Also covers the SAME-DAY cache-miss-is-authoritative follow-up: even the widened cache
left handoffs.collect() spawning ~308 per-path fallback subprocesses to re-confirm paths
that have ZERO git history under any candidate string, ever — a correct cache cannot
resolve those to True, so a miss against a COMPLETE cache is provably "never tracked"
and the fallback spawn is pure waste. GitHistoryCache.complete (set True only when the
priming pass is confirmed to cover the repo's full history — not shallow, not a
partial/filtered clone) licenses _memoized_ever_tracked to answer a miss authoritatively
without spawning. The classes below pin: the win (zero-spawn authoritative miss), and
the required fallback-preserving cases (None cache, shallow clone, an object with no
`.complete` attribute at all).

Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-boot-sweep-10s-timeout.md
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import dag
from coordinator_core.win_portability import (
    no_console_creationflags,
    no_console_passthrough_kwargs,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


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
        cwd=root, check=True, **no_console_passthrough_kwargs(),
    )


class TestRepeatedLookupSpawnsOnce:
    def test_positive_result_cached_after_first_spawn(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        _commit_file(root, "state/handoffs/foo.md")

        spawn_count = [0]
        orig_run = dag.subprocess.run

        def counting_run(argv, *a, **kw):
            spawn_count[0] += 1
            return orig_run(argv, *a, **kw)

        monkeypatch.setattr(dag.subprocess, "run", counting_run)

        results = [
            dag._git_path_ever_tracked("state/handoffs/foo.md", str(root))
            for _ in range(50)
        ]

        assert all(r is True for r in results), (
            f"expected every lookup to resolve True for a tracked path, got {results}"
        )
        assert spawn_count[0] == 1, (
            "expected exactly ONE git subprocess spawn across 50 repeat lookups of the "
            f"same path (memoized), got {spawn_count[0]}"
        )


class TestNegativeResultCached:
    def test_never_tracked_path_cached_after_first_spawn(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        _commit_file(root, "state/handoffs/other.md")

        spawn_count = [0]
        orig_run = dag.subprocess.run

        def counting_run(argv, *a, **kw):
            spawn_count[0] += 1
            return orig_run(argv, *a, **kw)

        monkeypatch.setattr(dag.subprocess, "run", counting_run)

        results = [
            dag._git_path_ever_tracked("archive/handoffs/never-existed.md", str(root))
            for _ in range(50)
        ]

        assert all(r is False for r in results), (
            f"expected every lookup to resolve False for a never-tracked path, got {results}"
        )
        assert spawn_count[0] == 1, (
            "expected exactly ONE git subprocess spawn across 50 repeat lookups of a "
            f"never-tracked path (negative result must be cached too), got {spawn_count[0]}"
        )


class TestInvalidationForcesRequery:
    def test_stale_negative_not_served_after_invalidate(self, tmp_path):
        root = _init_repo(tmp_path)
        _commit_file(root, "state/handoffs/seed.md")

        rel_path = "archive/handoffs/newly-tracked.md"

        assert dag._git_path_ever_tracked(rel_path, str(root)) is False

        assert dag._git_path_ever_tracked(rel_path, str(root)) is False

        _commit_file(root, rel_path)

        dag.invalidate_git_history_cache()

        assert dag._git_path_ever_tracked(rel_path, str(root)) is True, (
            "expected a re-query (not a stale cached False) for a path that became "
            "git-tracked after a mid-sweep commit, following invalidate_git_history_cache()"
        )

    def test_generation_bump_discards_prior_generation_entries(self, tmp_path):
        root = _init_repo(tmp_path)
        _commit_file(root, "state/handoffs/seed.md")

        rel_path = "state/handoffs/seed.md"
        dag._git_path_ever_tracked(rel_path, str(root))
        cache_key_gen0 = (dag._EVER_TRACKED_GENERATION, str(root), rel_path)
        assert cache_key_gen0 in dag._EVER_TRACKED_CACHE

        dag.invalidate_git_history_cache()

        assert cache_key_gen0 not in dag._EVER_TRACKED_CACHE, (
            "invalidate_git_history_cache() must discard prior-generation cache entries, "
            "not merely bump the generation counter"
        )


class TestBuildGitHistoryCacheWidening:
    def test_renamed_path_present_in_widened_cache(self, tmp_path):
        root = _init_repo(tmp_path)
        subprocess.run(
            ["git", "config", "diff.renames", "true"], cwd=root, check=True,
            **no_console_passthrough_kwargs(),
        )
        _commit_file(root, "state/handoffs/old-name.md")

        (root / "state/handoffs/old-name.md").rename(root / "state/handoffs/new-name.md")
        subprocess.run(
            ["git", "add", "-A"], cwd=root, check=True,
            **no_console_passthrough_kwargs(),
        )
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "rename old to new"],
            cwd=root, check=True,
            **no_console_passthrough_kwargs(),
        )

        cache = dag.build_git_history_cache(str(root))

        assert cache is not None, "expected build_git_history_cache to succeed against a real repo"
        assert "state/handoffs/new-name.md" in cache, (
            "widened cache must catch a path renamed INTO its final name — "
            f"got {sorted(cache)}"
        )
        assert "state/handoffs/old-name.md" in cache, (
            "widened cache must also retain the rename SOURCE path (it was tracked too) — "
            f"got {sorted(cache)}"
        )

    def test_modify_only_path_present_in_widened_cache(self, tmp_path):
        root = _init_repo(tmp_path)
        _commit_file(root, "state/handoffs/modified.md", content="v1")
        _commit_file(root, "state/handoffs/modified.md", content="v2")

        cache = dag.build_git_history_cache(str(root))

        assert cache is not None
        assert "state/handoffs/modified.md" in cache

    def test_widened_cache_short_circuits_the_per_path_fallback_spawn(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        subprocess.run(["git", "config", "diff.renames", "true"], cwd=root, check=True, **no_console_passthrough_kwargs())
        _commit_file(root, "state/handoffs/old-name.md")
        (root / "state/handoffs/old-name.md").rename(root / "state/handoffs/new-name.md")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "rename old to new"],
            cwd=root, check=True,
            **no_console_passthrough_kwargs(),
        )

        cache = dag.build_git_history_cache(str(root))
        assert cache is not None and "state/handoffs/new-name.md" in cache

        spawn_count = [0]
        orig_run = dag.subprocess.run

        def counting_run(argv, *a, **kw):
            spawn_count[0] += 1
            return orig_run(argv, *a, **kw)

        monkeypatch.setattr(dag.subprocess, "run", counting_run)

        memo: dict = {}
        result = dag._memoized_ever_tracked(
            "state/handoffs/new-name.md", memo, str(root), cache,
        )

        assert result is True
        assert spawn_count[0] == 0, (
            "expected the widened cache to resolve the renamed path without any "
            f"per-path git fallback spawn, got {spawn_count[0]} spawn(s)"
        )

    def test_cache_miss_still_falls_through_to_per_call_resolution(self, tmp_path):
        root = _init_repo(tmp_path)
        _commit_file(root, "state/handoffs/seed.md")
        _commit_file(root, "state/handoffs/only-in-fallback.md")

        sparse_cache = {"state/handoffs/seed.md"}

        memo: dict = {}
        result = dag._memoized_ever_tracked(
            "state/handoffs/only-in-fallback.md", memo, str(root), sparse_cache,
        )

        assert result is True, (
            "a path absent from the cache but genuinely git-tracked must still "
            "resolve True via the per-call fallback (miss = unknown, not absent)"
        )


#     A miss against a COMPLETE GitHistoryCache resolves False with ZERO

class TestCacheMissIsAuthoritativeWhenComplete:
    def test_fresh_repo_cache_reports_complete(self, tmp_path):
        root = _init_repo(tmp_path)
        _commit_file(root, "state/handoffs/seed.md")

        cache = dag.build_git_history_cache(str(root))

        assert cache is not None
        assert cache.complete is True, (
            "a freshly-init'd, non-shallow, non-partial repo must report a complete cache"
        )

    def test_miss_against_complete_cache_resolves_false_with_zero_spawns(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        _commit_file(root, "state/handoffs/seed.md")

        cache = dag.build_git_history_cache(str(root))
        assert cache is not None and cache.complete is True
        assert "archive/handoffs/never-existed.md" not in cache

        spawn_count = [0]
        orig_run = dag.subprocess.run

        def counting_run(argv, *a, **kw):
            spawn_count[0] += 1
            return orig_run(argv, *a, **kw)

        monkeypatch.setattr(dag.subprocess, "run", counting_run)

        memo: dict = {}
        result = dag._memoized_ever_tracked(
            "archive/handoffs/never-existed.md", memo, str(root), cache,
        )

        assert result is False
        assert spawn_count[0] == 0, (
            "expected a miss against a COMPLETE cache to resolve authoritatively "
            f"without any per-path fallback spawn, got {spawn_count[0]} spawn(s)"
        )

    def test_miss_against_none_cache_still_falls_through(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        _commit_file(root, "state/handoffs/seed.md")

        spawn_count = [0]
        orig_run = dag.subprocess.run

        def counting_run(argv, *a, **kw):
            spawn_count[0] += 1
            return orig_run(argv, *a, **kw)

        monkeypatch.setattr(dag.subprocess, "run", counting_run)

        memo: dict = {}
        result = dag._memoized_ever_tracked(
            "state/handoffs/seed.md", memo, str(root), None,
        )
        assert result is True, "a None cache must still resolve via per-call fallback"
        assert spawn_count[0] == 1, "a None cache must spawn the per-call fallback exactly once"

    def test_miss_against_bare_set_with_no_complete_attr_falls_through(self, tmp_path):
        root = _init_repo(tmp_path)
        _commit_file(root, "state/handoffs/seed.md")
        _commit_file(root, "state/handoffs/only-in-fallback.md")

        bare_cache = {"state/handoffs/seed.md"}
        assert not hasattr(bare_cache, "complete")

        memo: dict = {}
        result = dag._memoized_ever_tracked(
            "state/handoffs/only-in-fallback.md", memo, str(root), bare_cache,
        )
        assert result is True, (
            "a bare set (no .complete attribute) must never be treated as "
            "authoritative — a miss against it must fall through to the real answer"
        )

    def test_shallow_clone_reports_incomplete_and_preserves_fallback(self, tmp_path, monkeypatch):
        origin_parent = tmp_path / "origin_repo"
        origin_parent.mkdir()
        origin = _init_repo(origin_parent)
        _commit_file(origin, "state/handoffs/deleted-early.md")
        subprocess.run(
            ["git", "rm", "-q", "state/handoffs/deleted-early.md"], cwd=origin, check=True,
            **no_console_passthrough_kwargs(),
        )
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "remove deleted-early.md"],
            cwd=origin, check=True,
            **no_console_passthrough_kwargs(),
        )
        _commit_file(origin, "state/handoffs/second.md")

        shallow = tmp_path / "shallow_clone"
        subprocess.run(
            ["git", "clone", "--depth", "1", "--no-local", str(origin), str(shallow)],
            check=True, capture_output=True,
            **no_console_creationflags(),
        )
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=shallow, check=True, **no_console_passthrough_kwargs())
        subprocess.run(["git", "config", "user.name", "Test"], cwd=shallow, check=True, **no_console_passthrough_kwargs())
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=shallow, check=True, **no_console_passthrough_kwargs())

        assert dag._git_history_is_complete(str(shallow)) is False, (
            "a shallow clone must never report itself as a complete history"
        )

        cache = dag.build_git_history_cache(str(shallow))
        assert cache is not None
        assert cache.complete is False, (
            "build_git_history_cache must propagate the shallow-clone incompleteness "
            "onto the returned GitHistoryCache, not default it True"
        )
        assert "state/handoffs/deleted-early.md" not in cache, (
            "sanity check: the deleted-and-gone path must genuinely be a cache miss "
            "in the shallow clone, or this test isn't exercising the hazard it claims to"
        )

        spawn_count = [0]
        orig_run = dag.subprocess.run

        def counting_run(argv, *a, **kw):
            spawn_count[0] += 1
            return orig_run(argv, *a, **kw)

        monkeypatch.setattr(dag.subprocess, "run", counting_run)

        memo: dict = {}
        dag._memoized_ever_tracked(
            "state/handoffs/deleted-early.md", memo, str(shallow), cache,
        )
        assert spawn_count[0] == 1, (
            "expected the shallow clone's incomplete cache to force exactly one "
            f"per-call fallback spawn (never an authoritative miss), got {spawn_count[0]}"
        )

    def test_promisor_remote_reports_incomplete(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        _commit_file(root, "state/handoffs/seed.md")

        orig_run = dag.subprocess.run

        class _FakeCompletedProcess:
            def __init__(self, stdout: str, returncode: int = 0) -> None:
                self.stdout = stdout
                self.returncode = returncode

        def fake_run(argv, *a, **kw):
            if "config" in argv and "remote.origin.promisor" in argv:
                return _FakeCompletedProcess("true\n")
            return orig_run(argv, *a, **kw)

        monkeypatch.setattr(dag.subprocess, "run", fake_run)

        assert dag._git_history_is_complete(str(root)) is False, (
            "a promisor (partial/filtered clone) remote must report incomplete history"
        )
