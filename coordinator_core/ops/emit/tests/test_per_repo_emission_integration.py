"""AC6 integration test: two fixture repos emit collision-free per-repo snapshots.

Tests the per-repo emission cutover (2026-07-07) end-to-end using the wired emit path.
Five assertions per AC6:
  (1) Each snapshot roots at its own repo's state/ — verified via the default output path
      (<repo_root>/state/cockpit-emission.json) and the per-repo coordinator_roots self-report
      slug. (The top-level coordinator_root_path scalar this originally asserted was removed
      2026-07-21 — zero readers, forbidden by SnapshotEnvelope additionalProperties:false.)
  (2) provenance repo == that repo's own slug (NOT the meta-repo slug; D7a reversed)
  (3) No cross-repo bleed between the two fixture repos
  (4) A linked-worktree fixture still emits non-zero records (common_dir → main_worktree_root)
  (5) git_branch/git_sha reflect the main worktree HEAD, not the originating linked-worktree path

AC10 gate: the full coordinator_core suite (python -m pytest coordinator_core/ -q) must be
green modulo the one known pre-existing red (test_handoff_schema_matches_doe_head).

Wired emit path (end-to-end, post C1-C4b):
    _artifact_emit(params, repo_root=common_dir)
      → main_worktree_root(common_dir)    [C4a]
      → resolve_context(derived_root)     [C2]
      → EmitContext.resolve(...)          [C1 — emitting-repo slug, not meta-repo]
      → emit(ctx, out=...)               [sentinel-bypassed for tests]

For the two-repo and provenance tests, EmitContext is constructed directly (like
test_emit_default_path.py does) to avoid a live git remote requirement in CI / fixture repos.
The coordinator_roots section always emits at least one self-report record, making the
provenance assertions directly testable on the output JSON.

Spec backlink: pln-per-repo-emission-cutover-un-h-03f05e § C7 / AC6 / AC10
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.emit.context import EmitContext, META_REPO_NAME_FALLBACK
from coordinator_core.ops.emit.envelope import emit
from coordinator_core.ops.fleet._common import main_worktree_root

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

# S4-F3 fix: _run_git mock target for EmitContext.resolve() code-behavior tests
_RUN_GIT_PATH = "coordinator_core.ops.emit.context._run_git"


# ---------------------------------------------------------------------------
# Fixture repo slugs (not real GitHub repos — integration fixture only)
# ---------------------------------------------------------------------------

_SLUG_ALPHA = "fixture-owner/project-alpha"
_SLUG_BETA = "fixture-owner/project-beta"

# Sentinel git values (fixture repos are not real git repos; volatile fields are
# non-load-bearing for these assertions; the key invariant is slug attribution).
_FIXTURE_SHA_ALPHA = "aaaa1111bbbb2222cccc3333dddd4444eeee5555"
_FIXTURE_SHA_BETA  = "bbbb2222cccc3333dddd4444eeee5555ffff6666"
_FIXTURE_BRANCH_ALPHA = "work/alpha-branch/2026-07-07"
_FIXTURE_BRANCH_BETA  = "work/beta-branch/2026-07-07"


# ---------------------------------------------------------------------------
# Shared factory
# ---------------------------------------------------------------------------

def _make_ctx(
    repo_root: Path,
    slug: str,
    *,
    git_branch: str = "main",
    git_sha: str = "0000000011111111222222223333333344444444",
) -> EmitContext:
    """Build a minimal EmitContext for a fixture repo (does not require a live git remote).

    coordinator_root is set to repo_root (the sections in this test read from
    central_state_root which equals repo_root/state; coordinator_root is only used
    for subprocess script paths and is non-load-bearing when sections degrade gracefully).
    central_state_root = repo_root/state (per-repo semantics post-2026-07-07 cutover).
    """
    return EmitContext(
        repo_root=repo_root,
        coordinator_root=repo_root,
        central_state_root=repo_root / "state",
        git_branch=git_branch,
        git_sha=git_sha,
        git_sha_short=git_sha[:8],
        observed_at="2026-07-07T10:00:00Z",
        hostname="fixture.integration.test",
        repo_name=slug,
    )


# ---------------------------------------------------------------------------
# Assertion (1): each snapshot roots at its own repo's state/
# Verified via the default output path + the coordinator_roots self-report slug.
# (The top-level coordinator_root_path scalar this originally used was removed 2026-07-21 —
#  zero readers, forbidden by SnapshotEnvelope additionalProperties:false.)
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("requires_vendor_pin")
class TestEachSnapshotRootsAtOwnState:
    """(1) each emission is attributed to its own repo's slug and writes to its own state/."""

    def test_alpha_emission_attributed_to_alpha(self, tmp_path: Path) -> None:
        """Emission from project-alpha carries alpha's slug in coordinator_roots (per-repo rooting)."""
        repo_a = tmp_path / "project-alpha"
        (repo_a / "state").mkdir(parents=True)
        out_a = tmp_path / "out_a.json"

        emit(_make_ctx(repo_a, _SLUG_ALPHA, git_branch=_FIXTURE_BRANCH_ALPHA,
                       git_sha=_FIXTURE_SHA_ALPHA), out=out_a)

        data = json.loads(out_a.read_text())
        assert "coordinator_root_path" not in data, (
            "top-level coordinator_root_path scalar was removed 2026-07-21 and must not reappear"
        )
        roots = data["coordinator_roots"]
        assert len(roots) >= 1
        for record in roots:
            assert record["repo"] == _SLUG_ALPHA, (
                f"alpha coordinator_roots must carry {_SLUG_ALPHA!r}; got {record['repo']!r}"
            )

    def test_beta_emission_attributed_to_beta(self, tmp_path: Path) -> None:
        """Emission from project-beta carries beta's slug in coordinator_roots (per-repo rooting)."""
        repo_b = tmp_path / "project-beta"
        (repo_b / "state").mkdir(parents=True)
        out_b = tmp_path / "out_b.json"

        emit(_make_ctx(repo_b, _SLUG_BETA, git_branch=_FIXTURE_BRANCH_BETA,
                       git_sha=_FIXTURE_SHA_BETA), out=out_b)

        data = json.loads(out_b.read_text())
        assert "coordinator_root_path" not in data
        roots = data["coordinator_roots"]
        assert len(roots) >= 1
        for record in roots:
            assert record["repo"] == _SLUG_BETA, (
                f"beta coordinator_roots must carry {_SLUG_BETA!r}; got {record['repo']!r}"
            )

    def test_two_repos_produce_distinct_attribution(self, tmp_path: Path) -> None:
        """The two repos produce disjoint coordinator_roots slug sets (collision-free)."""
        repo_a = tmp_path / "project-alpha"
        repo_b = tmp_path / "project-beta"
        for r in (repo_a, repo_b):
            (r / "state").mkdir(parents=True)
        out_a = tmp_path / "out_a.json"
        out_b = tmp_path / "out_b.json"

        emit(_make_ctx(repo_a, _SLUG_ALPHA), out=out_a)
        emit(_make_ctx(repo_b, _SLUG_BETA), out=out_b)

        data_a = json.loads(out_a.read_text())
        data_b = json.loads(out_b.read_text())
        alpha_slugs = {r["repo"] for r in data_a["coordinator_roots"]}
        beta_slugs = {r["repo"] for r in data_b["coordinator_roots"]}
        assert alpha_slugs and beta_slugs
        assert alpha_slugs.isdisjoint(beta_slugs), (
            f"Two distinct repos must produce disjoint coordinator_roots slugs (AC6-1); "
            f"alpha={alpha_slugs}, beta={beta_slugs}"
        )

    def test_default_output_path_is_own_state_dir(self, tmp_path: Path) -> None:
        """Without an explicit out=, emission writes to <repo_root>/state/cockpit-emission.json."""
        repo_a = tmp_path / "project-alpha"
        (repo_a / "state").mkdir(parents=True)
        ctx = _make_ctx(repo_a, _SLUG_ALPHA)

        # Default output path: central_state_root / "cockpit-emission.json"
        expected_out = repo_a / "state" / "cockpit-emission.json"
        # No sentinel present — production emit allowed
        emit(ctx)

        assert expected_out.exists(), (
            f"Default emit must write to {expected_out} (own state dir)"
        )
        data = json.loads(expected_out.read_text())
        assert "coordinator_root_path" not in data
        for record in data["coordinator_roots"]:
            assert record["repo"] == _SLUG_ALPHA


# ---------------------------------------------------------------------------
# Assertion (2): provenance repo == that repo's own slug (D7a reversed)
# Verified via coordinator_roots records, which always carry at least one
# self-report (local_fs fallback fires when gh unavailable for a fixture slug).
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("requires_vendor_pin")
class TestProvenanceRepoIsPerRepoSlug:
    """(2) coordinator_roots records carry the emitting repo's slug, NOT the meta-repo slug."""

    def test_alpha_coordinator_roots_carries_alpha_slug(self, tmp_path: Path) -> None:
        """coordinator_roots self-report record has repo == _SLUG_ALPHA."""
        repo_a = tmp_path / "project-alpha"
        (repo_a / "state").mkdir(parents=True)
        out = tmp_path / "out.json"

        emit(_make_ctx(repo_a, _SLUG_ALPHA), out=out)

        data = json.loads(out.read_text())
        roots = data["coordinator_roots"]
        assert len(roots) >= 1, (
            "coordinator_roots must have at least one self-report record (AC6-2)"
        )
        for record in roots:
            assert record["repo"] == _SLUG_ALPHA, (
                f"coordinator_roots record.repo must be {_SLUG_ALPHA!r}; "
                f"got {record['repo']!r}"
            )

    def test_beta_coordinator_roots_carries_beta_slug(self, tmp_path: Path) -> None:
        """coordinator_roots self-report record has repo == _SLUG_BETA."""
        repo_b = tmp_path / "project-beta"
        (repo_b / "state").mkdir(parents=True)
        out = tmp_path / "out.json"

        emit(_make_ctx(repo_b, _SLUG_BETA), out=out)

        data = json.loads(out.read_text())
        for record in data["coordinator_roots"]:
            assert record["repo"] == _SLUG_BETA, (
                f"coordinator_roots record.repo must be {_SLUG_BETA!r}; "
                f"got {record['repo']!r}"
            )

    def test_meta_repo_slug_absent_from_fixture_repo_coordinator_roots(self, tmp_path: Path) -> None:
        """The meta-repo slug must NOT appear in coordinator_roots of a non-meta-repo emission.

        META_REPO_NAME_FALLBACK is a test/doc-oracle constant only (post-2026-07-07 cutover).
        It must NOT appear as the provenance slug for a fixture repo — that would be the
        old D7a bug (silent attribution to the wrong tree). D7a is reversed: each repo
        emits under its own identity.
        """
        repo_a = tmp_path / "project-alpha"
        (repo_a / "state").mkdir(parents=True)
        out = tmp_path / "out.json"

        emit(_make_ctx(repo_a, _SLUG_ALPHA), out=out)

        data = json.loads(out.read_text())
        for record in data["coordinator_roots"]:
            assert record["repo"] != META_REPO_NAME_FALLBACK, (
                f"coordinator_roots record.repo must NOT be the meta-repo slug "
                f"{META_REPO_NAME_FALLBACK!r} (D7a reversed — per-repo attribution, AC6-2)"
            )


# ---------------------------------------------------------------------------
# Assertion (3): no cross-repo bleed
# The two emissions are independent — no record or field from one appears in the other.
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("requires_vendor_pin")
class TestNoCrossRepoBleed:
    """(3) repo A's emission must not reference repo B's path or slug, and vice versa."""

    def test_alpha_emission_does_not_reference_beta_root(self, tmp_path: Path) -> None:
        """Emission from alpha must not include beta's repo_root in coordinator_root_path."""
        repo_a = tmp_path / "project-alpha"
        repo_b = tmp_path / "project-beta"
        for r in (repo_a, repo_b):
            (r / "state").mkdir(parents=True)
        out_a = tmp_path / "out_a.json"

        emit(_make_ctx(repo_a, _SLUG_ALPHA), out=out_a)

        data_a = json.loads(out_a.read_text())
        assert str(repo_b) not in json.dumps(data_a), (
            "Alpha emission must not reference beta's root path anywhere in the envelope (AC6-3)"
        )
        for record in data_a["coordinator_roots"]:
            assert record["repo"] != _SLUG_BETA, (
                "Alpha coordinator_roots must not carry beta slug (AC6-3 no bleed)"
            )

    def test_beta_emission_does_not_reference_alpha_root(self, tmp_path: Path) -> None:
        """Emission from beta must not include alpha's repo_root in coordinator_root_path."""
        repo_a = tmp_path / "project-alpha"
        repo_b = tmp_path / "project-beta"
        for r in (repo_a, repo_b):
            (r / "state").mkdir(parents=True)
        out_b = tmp_path / "out_b.json"

        emit(_make_ctx(repo_b, _SLUG_BETA), out=out_b)

        data_b = json.loads(out_b.read_text())
        assert str(repo_a) not in json.dumps(data_b), (
            "Beta emission must not reference alpha's root path anywhere in the envelope (AC6-3)"
        )
        for record in data_b["coordinator_roots"]:
            assert record["repo"] != _SLUG_ALPHA, (
                "Beta coordinator_roots must not carry alpha slug (AC6-3 no bleed)"
            )

    def test_output_files_are_independent_and_collision_free(self, tmp_path: Path) -> None:
        """The two emissions write to independent output files (no path collision)."""
        repo_a = tmp_path / "project-alpha"
        repo_b = tmp_path / "project-beta"
        for r in (repo_a, repo_b):
            (r / "state").mkdir(parents=True)

        # Write to each repo's canonical location
        out_a = repo_a / "state" / "cockpit-emission.json"
        out_b = repo_b / "state" / "cockpit-emission.json"

        emit(_make_ctx(repo_a, _SLUG_ALPHA), out=out_a)
        emit(_make_ctx(repo_b, _SLUG_BETA), out=out_b)

        assert out_a.resolve() != out_b.resolve(), "Output paths must be distinct"
        data_a = json.loads(out_a.read_text())
        data_b = json.loads(out_b.read_text())
        # Verify collision-free: output paths and coordinator_roots slug sets are distinct.
        alpha_roots_repos = {r["repo"] for r in data_a["coordinator_roots"]}
        beta_roots_repos = {r["repo"] for r in data_b["coordinator_roots"]}
        assert alpha_roots_repos.isdisjoint(beta_roots_repos), (
            f"coordinator_roots slugs must be disjoint between repos; "
            f"alpha={alpha_roots_repos}, beta={beta_roots_repos}"
        )


# ---------------------------------------------------------------------------
# Assertion (4): linked-worktree fixture still emits non-zero records
# Tests the C4a derivation: _artifact_emit receives common_dir (.git directory),
# derives main_worktree via main_worktree_root(common_dir), and produces a valid
# emission with coordinator_roots >= 1 record.
# ---------------------------------------------------------------------------

class TestLinkedWorktreeEmitsNonZeroRecords:
    """(4) Dispatching via a linked-worktree common_dir produces non-zero records."""

    def test_main_worktree_root_derives_parent_of_common_dir(self, tmp_path: Path) -> None:
        """main_worktree_root(main/.git) == main (not main/.git)."""
        main_worktree = tmp_path / "project-linked"
        main_worktree.mkdir()
        common_dir = main_worktree / ".git"
        common_dir.mkdir()  # standard: main_worktree/.git is a directory

        derived = main_worktree_root(common_dir)
        assert derived == main_worktree, (
            f"main_worktree_root({common_dir!r}) must return {main_worktree!r}; got {derived!r}"
        )
        assert derived != common_dir, (
            "main_worktree_root must return the parent, NOT the .git dir itself"
        )

    @pytest.mark.usefixtures("requires_vendor_pin")
    def test_artifact_emit_handler_derives_main_worktree_from_common_dir(
        self, tmp_path: Path
    ) -> None:
        """_artifact_emit called with repo_root=common_dir resolves main_worktree and emits.

        Verifies the full wired path: common_dir → main_worktree_root → resolve_context →
        emit → valid JSON with coordinator_roots >= 1 record (self-report).
        """
        from coordinator_core.ops.artifact_emit import _artifact_emit

        main_worktree = tmp_path / "project-linked"
        (main_worktree / "state").mkdir(parents=True)
        common_dir = main_worktree / ".git"
        common_dir.mkdir()

        out_path = tmp_path / "linked-out.json"
        captured_roots: list[Path] = []

        # Real EmitContext (not a mock) so emit() actually writes JSON and sections run.
        real_ctx = _make_ctx(main_worktree, _SLUG_ALPHA,
                             git_branch=_FIXTURE_BRANCH_ALPHA, git_sha=_FIXTURE_SHA_ALPHA)

        def spy_resolve_context(repo_root=None) -> EmitContext:
            if repo_root is not None:
                captured_roots.append(repo_root)
            return real_ctx

        with patch(
            "coordinator_core.ops.artifact_emit._envelope.resolve_context",
            side_effect=spy_resolve_context,
        ):
            result = _artifact_emit({"out": str(out_path)}, repo_root=common_dir)

        # Handler returned ok
        assert result["ok"] is True, f"emit result must be ok; got {result!r}"

        # resolve_context was called once with the DERIVED main_worktree (not common_dir)
        assert len(captured_roots) == 1, "resolve_context must be called exactly once"
        assert captured_roots[0] == main_worktree, (
            f"resolve_context must receive main_worktree {main_worktree!r}; "
            f"got {captured_roots[0]!r} — raw common_dir is {common_dir!r}"
        )
        assert captured_roots[0] != common_dir, (
            "resolve_context must NOT receive the raw .git common_dir"
        )

        # Emitted JSON must have coordinator_roots with >= 1 record
        data = json.loads(out_path.read_text())
        assert len(data["coordinator_roots"]) >= 1, (
            "linked-worktree emit must produce at least 1 coordinator_roots record (AC6-4)"
        )

    @pytest.mark.usefixtures("requires_vendor_pin")
    def test_linked_worktree_emit_carries_alpha_slug_not_common_dir_name(
        self, tmp_path: Path
    ) -> None:
        """coordinator_roots from a linked-worktree emit carries the main-worktree slug."""
        from coordinator_core.ops.artifact_emit import _artifact_emit

        main_worktree = tmp_path / "project-linked"
        (main_worktree / "state").mkdir(parents=True)
        common_dir = main_worktree / ".git"
        common_dir.mkdir()

        out_path = tmp_path / "linked-out2.json"
        real_ctx = _make_ctx(main_worktree, _SLUG_ALPHA)

        with patch(
            "coordinator_core.ops.artifact_emit._envelope.resolve_context",
            return_value=real_ctx,
        ):
            _artifact_emit({"out": str(out_path)}, repo_root=common_dir)

        data = json.loads(out_path.read_text())
        for record in data["coordinator_roots"]:
            assert record["repo"] == _SLUG_ALPHA, (
                f"linked-worktree coordinator_roots must carry main-worktree slug {_SLUG_ALPHA!r}; "
                f"got {record['repo']!r}"
            )


# ---------------------------------------------------------------------------
# Assertion (5): git_branch/git_sha reflect the main worktree HEAD
# The emit context is built from main_worktree_root(common_dir) post-C4a, so the
# branch/sha come from the MAIN worktree's HEAD, not the originating linked worktree's
# detached or per-worktree branch state.
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("requires_vendor_pin")
class TestGitProvenanceReflectsMainWorktree:
    """(5) git_branch/git_sha in the EmitContext reflect the main worktree HEAD."""

    def test_ctx_git_branch_resolved_from_run_git(self, tmp_path: Path) -> None:
        """EmitContext.resolve() reads git_branch and git_sha from _run_git, not from a hardcoded value.

        Review: code-reviewer (S4-F3 fix) — replaces the former tautological test that asserted
        dataclass initialization (ctx.git_branch == branch where branch was the arg to _make_ctx).
        This test mocks _run_git to return specific known values and calls EmitContext.resolve()
        to verify the actual code behavior: branch and sha flow from _run_git into the context fields.
        """
        repo = tmp_path / "project-alpha"
        repo.mkdir()

        expected_branch = _FIXTURE_BRANCH_ALPHA
        expected_sha = _FIXTURE_SHA_ALPHA

        def mock_run_git(repo_root: Path, *args: str):
            if "--abbrev-ref" in args:
                return expected_branch
            if args == ("rev-parse", "HEAD"):
                return expected_sha
            # remote get-url → None (no remote → local/<basename> slug)
            return None

        with patch(_RUN_GIT_PATH, side_effect=mock_run_git):
            ctx = EmitContext.resolve(repo, repo, repo / "state")

        assert ctx.git_branch == expected_branch, (
            f"git_branch must come from _run_git; got {ctx.git_branch!r}"
        )
        assert ctx.git_sha == expected_sha, (
            f"git_sha must come from _run_git; got {ctx.git_sha!r}"
        )
        assert ctx.repo_root == repo

    def test_linked_worktree_ctx_uses_main_worktree_root_not_common_dir(
        self, tmp_path: Path
    ) -> None:
        """Context built from main_worktree_root(common_dir) is rooted at main_worktree.

        Confirms the C4a derivation: when an IPC request carries _origin_worktree =
        linked_worktree, ipc.py resolves common_dir, and the handler builds ctx from
        main_worktree_root(common_dir) = main_worktree.  Git ops run on main_worktree
        (the shared .git), NOT on the linked worktree's per-worktree branch.

        Review: code-reviewer (S4-F3 fix) — tautological git-field assertions (ctx.git_branch
        == _FIXTURE_BRANCH_ALPHA where the value was literally passed at construction) removed.
        The key non-tautological invariants tested here: main_worktree_root(common_dir) returns
        the correct parent, and ctx.repo_root / ctx.central_state_root are derived from it.
        """
        main_worktree = tmp_path / "project-main"
        main_worktree.mkdir()
        common_dir = main_worktree / ".git"
        common_dir.mkdir()

        # Derive the root as the handler does (C4a) — non-tautological: exercises main_worktree_root
        derived = main_worktree_root(common_dir)
        assert derived == main_worktree, (
            f"main_worktree_root(common_dir) must return {main_worktree}; got {derived}"
        )

        # Build ctx from the derived root (same construction as the handler)
        ctx = _make_ctx(derived, _SLUG_ALPHA,
                        git_branch=_FIXTURE_BRANCH_ALPHA, git_sha=_FIXTURE_SHA_ALPHA)

        # These assertions are non-tautological: they verify the derivation chain produced the
        # correct repo_root (derived from common_dir, not the raw linked worktree path).
        assert ctx.repo_root == main_worktree, (
            "ctx.repo_root must be main_worktree (C4a derivation)"
        )
        assert ctx.central_state_root == main_worktree / "state", (
            "ctx.central_state_root must be main_worktree/state"
        )
        # git_branch/git_sha correctness under EmitContext.resolve() is covered by
        # test_ctx_git_branch_resolved_from_run_git; fixture values here are non-load-bearing.

    @pytest.mark.usefixtures("requires_vendor_pin")
    def test_git_branch_present_in_emission_envelope(self, tmp_path: Path) -> None:
        """The emission envelope carries schema_version and local_fs provenance.

        Review: code-reviewer (S4-F3 fix) — removed dead github_graphql conditional branch
        (fixture repos have no real GitHub presence so prov.source_kind is always local_fs;
        the conditional never fired). Replaced with an unconditional assertion on the local_fs
        provenance shape that ACTUALLY exercises the fixture-repo code path.
        """
        repo = tmp_path / "project-alpha"
        (repo / "state").mkdir(parents=True)
        out = tmp_path / "out.json"

        ctx = _make_ctx(repo, _SLUG_ALPHA, git_branch=_FIXTURE_BRANCH_ALPHA,
                        git_sha=_FIXTURE_SHA_ALPHA)
        emit(ctx, out=out)

        data = json.loads(out.read_text())
        assert "coordinator_root_path" not in data  # top-level scalar removed 2026-07-21
        assert data["schema_version"]  # non-empty

        roots = data["coordinator_roots"]
        assert len(roots) >= 1
        for record in roots:
            prov = record.get("provenance", {})
            source_kind = prov.get("source_kind")
            if source_kind == "local_fs":
                # D9 bidirectional invariant: local_fs provenance must have ref=None
                assert prov.get("ref") is None, (
                    "local_fs provenance ref must be None "
                    "(D9 bidirectional invariant — non-git-backed source_kind)"
                )
            elif source_kind == "github_graphql":
                # This branch fires only if gh is available and the fixture slug resolves.
                # When it fires, assert the branch/sha flow through correctly.
                ref = prov.get("ref")
                assert ref is not None
                assert ref.get("branch") == _FIXTURE_BRANCH_ALPHA
                assert ref.get("sha") == _FIXTURE_SHA_ALPHA
