"""Tests for the shared memoized repo-root resolver
(`coordinator_core.write_guards._repo_root`) and the three write guards
migrated onto it in chunk C3
(docs/plans/2026-08-07-no-window-subprocess-primitive.md).

Covers (a) the resolver is invoked once (no duplicate spawn) across a
multi-guard Write against the same cwd -- the delegate's own process-scoped
memo already guarantees this, so this test pins that guarantee is actually
reached through the write_guards seam, not just true of the delegate in
isolation -- and (b) each migrated guard still returns its prior verdict
unchanged post-migration.

Spec backlink: pln-no-window-subprocess-primitive-750d2d § C3
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import repo_root as _git_repo_root
from coordinator_core.write_guards import _repo_root
from coordinator_core.write_guards import block_consumed_handoff_edit as _handoff_guard
from coordinator_core.write_guards import (
    block_cutover_phase_hand_edit as _cutover_guard,
)
from coordinator_core.write_guards import (
    block_memo_status_hand_edit as _memo_status_guard,
)
from coordinator_core.write_guards import (
    block_subagent_archive_write as _archive_guard,
)
from coordinator_core.write_guards import (
    block_subagent_plan_body_write as _plan_body_guard,
)
from coordinator_core.write_guards import (
    bump_out_of_repo_tool_write as _bump_guard,
)
from coordinator_core.write_guards import guard_doctrine_surface_edits as _doctrine_guard
from coordinator_core.write_guards import (
    validate_frontmatter_schema_advisory as _advisory_guard,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


@pytest.fixture
def scratch_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, **_NO_CONSOLE)
    return repo


@pytest.fixture(autouse=True)
def _clear_repo_root_memo():
    """Each test gets a fresh delegate memo, so an earlier test's cwd
    resolution can never mask a regression in a later test.
    """
    _git_repo_root.clear_memo()
    yield
    _git_repo_root.clear_memo()


class TestSharedResolver:
    def test_resolves_repo_root_for_a_real_repo(self, scratch_repo):
        resolved = _repo_root.resolve_repo_root(str(scratch_repo))
        assert resolved is not None
        # Compare resolved (real, symlink-free) paths -- a scratch tmp_path
        # root may itself be a symlink on some hosts (e.g. macOS /tmp).
        import os

        assert os.path.realpath(resolved) == os.path.realpath(str(scratch_repo))

    def test_returns_none_outside_any_repo(self, tmp_path):
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        assert _repo_root.resolve_repo_root(str(outside)) is None

    def test_ordinary_repo_walk_never_spawns(self, scratch_repo, monkeypatch):
        """Multiple guards resolving the SAME ordinary-repo cwd within one
        process must hit the walk-based path, not the spawn fallback at
        all -- names what it actually checks (walk-only, no spawn), not the
        memoization guarantee (see
        `test_spawn_fallback_memoizes_across_repeat_calls_for_same_cwd`
        below for that).
        """
        spawn_calls = []
        real_spawn = _git_repo_root._spawn_rev_parse

        def _counting_spawn(args, cwd):
            spawn_calls.append((tuple(args), cwd))
            return real_spawn(args, cwd)

        monkeypatch.setattr(_git_repo_root, "_spawn_rev_parse", _counting_spawn)

        # The walk-based path finds `.git` directly and never spawns at all
        # for an ordinary repo -- exercise the resolver 3x here to prove
        # that repeat, same case, none of the three trigger the delegate's
        # own spawn path either.
        first = _repo_root.resolve_repo_root(str(scratch_repo))
        second = _repo_root.resolve_repo_root(str(scratch_repo))
        third = _repo_root.resolve_repo_root(str(scratch_repo))

        assert first == second == third
        assert spawn_calls == []

    def test_spawn_fallback_memoizes_across_repeat_calls_for_same_cwd(
        self, scratch_repo, monkeypatch
    ):
        """Pins the "no duplicate spawn" guarantee the prior version of this
        test claimed to cover but didn't: forces `_walk_for_dot_git` to find
        nothing (as if no `.git` entry were discoverable), so every one of
        three same-cwd resolutions WOULD spawn if `_spawn_cached`'s memo
        were broken. Asserts only one spawn actually happens.

        Review finding (coordinatorcode-reviewer-a0de0949.md, P3): the prior
        version's own docstring admitted the walk always succeeds for an
        ordinary repo, so its `spawn_calls == []` assertion was trivially
        true regardless of whether memoization worked -- no regression in
        `_spawn_cached` would have been caught.
        """
        monkeypatch.setattr(_git_repo_root, "_walk_for_dot_git", lambda start: None)

        spawn_calls = []
        real_spawn = _git_repo_root._spawn_rev_parse

        def _counting_spawn(args, cwd):
            spawn_calls.append((tuple(args), cwd))
            return real_spawn(args, cwd)

        monkeypatch.setattr(_git_repo_root, "_spawn_rev_parse", _counting_spawn)

        first = _repo_root.resolve_repo_root(str(scratch_repo))
        second = _repo_root.resolve_repo_root(str(scratch_repo))
        third = _repo_root.resolve_repo_root(str(scratch_repo))

        assert first == second == third
        assert first is not None  # the spawn fallback DID resolve a root
        assert len(spawn_calls) == 1


class TestMigratedGuardVerdictsUnchanged:
    def test_doctrine_guard_git_root_resolves_inside_scratch_repo(self, scratch_repo, monkeypatch):
        monkeypatch.chdir(scratch_repo)
        root = _doctrine_guard._git_root()
        assert root is not None

    def test_doctrine_guard_git_root_none_outside_repo(self, tmp_path, monkeypatch):
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        monkeypatch.chdir(outside)
        assert _doctrine_guard._git_root() is None

    def test_doctrine_guard_allows_unprotected_file(self, scratch_repo):
        payload = {
            "tool_name": "Write",
            "cwd": str(scratch_repo),
            "tool_input": {
                "file_path": str(scratch_repo / "some-ordinary-file.md"),
                "content": "hello",
            },
        }
        assert _doctrine_guard.check(payload) is None

    def test_advisory_resolve_repo_root_returns_repo_root_for_real_repo(self, scratch_repo):
        resolved = _advisory_guard.resolve_repo_root(str(scratch_repo))
        import os

        assert os.path.realpath(resolved) == os.path.realpath(str(scratch_repo))

    def test_advisory_resolve_repo_root_falls_back_to_cwd_outside_repo(self, tmp_path):
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        assert _advisory_guard.resolve_repo_root(str(outside)) == str(outside)


class TestC3bMigratedGuardVerdictsUnchanged:
    """Chunk C3b (docs/plans/2026-08-07-no-window-subprocess-primitive.md):
    the six guards whose own ``_resolve_git_root`` now delegates to
    ``write_guards._repo_root.resolve_repo_root`` instead of a hand-rolled
    ``subprocess.run``. Pins the same fail-open-to-``None``/resolves-inside-
    a-repo contract each guard's own ``_resolve_git_root`` docstring already
    claimed pre-migration -- a regression here would mean the migration
    silently changed a guard's ALLOW/DENY verdict, not just its spawn count.
    """

    @pytest.mark.parametrize(
        "guard_module",
        [
            _handoff_guard,
            _cutover_guard,
            _memo_status_guard,
            _archive_guard,
            _plan_body_guard,
            _bump_guard,
        ],
        ids=[
            "block_consumed_handoff_edit",
            "block_cutover_phase_hand_edit",
            "block_memo_status_hand_edit",
            "block_subagent_archive_write",
            "block_subagent_plan_body_write",
            "bump_out_of_repo_tool_write",
        ],
    )
    def test_resolve_git_root_resolves_inside_scratch_repo(self, guard_module, scratch_repo):
        import os

        resolved = guard_module._resolve_git_root(str(scratch_repo))
        assert resolved is not None
        assert os.path.realpath(resolved) == os.path.realpath(str(scratch_repo))

    @pytest.mark.parametrize(
        "guard_module",
        [
            _handoff_guard,
            _cutover_guard,
            _memo_status_guard,
            _archive_guard,
            _plan_body_guard,
            _bump_guard,
        ],
        ids=[
            "block_consumed_handoff_edit",
            "block_cutover_phase_hand_edit",
            "block_memo_status_hand_edit",
            "block_subagent_archive_write",
            "block_subagent_plan_body_write",
            "bump_out_of_repo_tool_write",
        ],
    )
    def test_resolve_git_root_none_outside_any_repo(self, guard_module, tmp_path):
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        assert guard_module._resolve_git_root(str(outside)) is None

    def test_memo_status_guard_git_common_dir_still_resolves_via_no_console_creationflags(
        self, scratch_repo
    ):
        """``_resolve_git_common_dir`` is NOT an AC4 site (different git
        form), but its hand-rolled ``creationflags`` literal was routed
        through the shared ``no_console_creationflags()`` helper in this
        same chunk -- pins that it still resolves correctly post-migration.
        """
        import os

        resolved = _memo_status_guard._resolve_git_common_dir(str(scratch_repo))
        assert resolved is not None
        assert os.path.realpath(resolved) == os.path.realpath(
            str(scratch_repo / ".git")
        )

    def test_plan_body_guard_git_dir_still_resolves_via_no_console_creationflags(
        self, scratch_repo
    ):
        """``_resolve_git_dir`` is NOT an AC4 site (``--git-dir``, not
        ``--show-toplevel``) but was also routed onto ``no_console_
        creationflags()`` in this chunk -- pins it still resolves post-migration.

        D4 (docs/plans/2026-08-07-spawn-storm-culprit-taxonomy-and-detectors.md)
        replaced the hand-rolled ``git rev-parse --git-dir`` spawn behind
        this function with the non-spawning ``coordinator_core.git.git_dir.
        resolve_git_dir`` seam -- see ``TestD4MigratedGitDirResolversUnchanged``
        below for the dedicated real-body coverage that pins the resulting
        (now-always-absolute) output.
        """
        import os

        resolved = _plan_body_guard._resolve_git_dir(str(scratch_repo))
        assert resolved is not None
        assert os.path.basename(os.path.normpath(resolved)) == ".git"

    def test_handoff_guard_allows_unprotected_write_outside_any_handoff(self, scratch_repo):
        """End-to-end smoke: the migrated resolver composes correctly inside
        a real ``check()`` call, not just in isolation."""
        payload = {
            "tool_name": "Write",
            "cwd": str(scratch_repo),
            "tool_input": {
                "file_path": str(scratch_repo / "some-ordinary-file.md"),
                "content": "hello",
            },
        }
        assert _handoff_guard.check(payload) is None

    def test_archive_guard_allows_top_level_em_write(self, scratch_repo):
        """No ``agent_id`` -> top-level EM write -> allow, unaffected by the
        migrated resolver (which this payload never reaches)."""
        payload = {
            "tool_name": "Write",
            "cwd": str(scratch_repo),
            "tool_input": {
                "file_path": str(scratch_repo / "archive" / "x.md"),
                "content": "hello",
            },
        }
        assert _archive_guard.check(payload) is None


class TestD4MigratedGitDirResolversUnchanged:
    """Chunk D4 (docs/plans/2026-08-07-spawn-storm-culprit-taxonomy-and-
    detectors.md): ``block_memo_status_hand_edit._resolve_git_common_dir``
    and ``block_subagent_plan_body_write._resolve_git_dir`` moved off their
    own hand-rolled ``git rev-parse --git-common-dir``/``--git-dir`` spawns
    onto the non-spawning ``coordinator_core.git.git_dir`` seam (fed by the
    already-shared, already-absolute ``resolve_repo_root``).

    Exercises the REAL, non-monkeypatched body of both functions against a
    genuine ``git init`` repo -- not a hand-mocked ``.git`` directory and not
    the CWD-inherited fixtures used elsewhere in this suite, so this passes
    from any checkout. Pins the chunk's correctness property directly
    ("each migrated guard returns its prior verdict unchanged" reframed at
    the resolver layer: a resolved absolute path inside a repo, ``None``
    outside one) rather than leaving it to a one-off smoke script no one
    can re-run.
    """

    def test_memo_status_common_dir_resolves_absolute_inside_repo(self, scratch_repo):
        import os

        resolved = _memo_status_guard._resolve_git_common_dir(str(scratch_repo))
        assert resolved is not None
        assert os.path.isabs(resolved)
        assert os.path.realpath(resolved) == os.path.realpath(str(scratch_repo / ".git"))

    def test_memo_status_common_dir_none_outside_any_repo(self, tmp_path):
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        assert _memo_status_guard._resolve_git_common_dir(str(outside)) is None

    def test_plan_body_git_dir_resolves_absolute_inside_repo(self, scratch_repo):
        import os

        resolved = _plan_body_guard._resolve_git_dir(str(scratch_repo))
        assert resolved is not None
        assert os.path.isabs(resolved)
        assert os.path.realpath(resolved) == os.path.realpath(str(scratch_repo / ".git"))

    def test_plan_body_git_dir_none_outside_any_repo(self, tmp_path):
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        assert _plan_body_guard._resolve_git_dir(str(outside)) is None
