"""Tests for C4b: canonicalize() wired into deliverable_status._compute_map / stamp.

Verifies the join-key transform closes the split-spine harm for the
deliverable_status.py consumer named in AC6/AC6b: a group of records split across a
declared fork pair (loser + winner deliverable_id) produces ONE grouped
``deliverable_status`` rather than two independently-computed, potentially
contradictory ones.

Fixture: `sat-01`'s own declared fork pair (`dlv-sat-01` winner /
`dlv-sat-01-sovereign-tracker-substrate-locke-02c8bc` loser), per AC6b. The equivalence
map is built directly in each test (via a `state/deliverable-equivalence.yaml` written
under `tmp_path`, or passed straight as a dict to `_compute_map`) — never read from the
repo's real artifact, which is C3b's concern and may not exist while this chunk runs.

Spec backlink: docs/plans/2026-08-01-deliverable-id-fork-remediation.md § C4 (AC6, AC6b)
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops import deliverable_equivalence as _equiv
from coordinator_core.ops.emit import deliverable_status
from coordinator_core.ops.emit import envelope as _envelope
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.envelope import _RegisteredSection

_WINNER = "dlv-sat-01"
_LOSER = "dlv-sat-01-sovereign-tracker-substrate-locke-02c8bc"


def _reset_memo() -> None:
    _equiv._reset_equivalence_map_cache()


def _write_equivalence_artifact(worktree_root: Path) -> None:
    state_dir = worktree_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "deliverable-equivalence.yaml").write_text(
        "entries:\n"
        f"  - loser: {_LOSER}\n"
        f"    winner: {_WINNER}\n"
        "    evidence: \"artifact-creation order test fixture\"\n",
        encoding="utf-8",
    )


class TestComputeMapForkGrouping:
    """_compute_map groups both fork legs under the winner id when given an equivalence map."""

    def test_split_records_produce_one_grouped_status(self) -> None:
        """Records split across the declared fork pair group under ONE canonical key."""
        equivalence_map = {_LOSER: _WINNER}

        # One leg (loser) carries a shipped handoff; the other leg (winner) carries only
        # an in-progress plan. Pre-fix (raw comparison) these are two independent groups:
        # the loser group resolves "shipped", the winner group resolves "in-progress" —
        # a contradiction. Post-fix they must collapse into one "shipped" group.
        handoffs = [{"deliverable_id": _LOSER, "shipped_sha": "abc123", "deployment_state": "shipped"}]
        plans = [{"deliverable_id": _WINNER, "status": "executing"}]
        roadmaps: list[dict] = []

        dlv_map = deliverable_status._compute_map(handoffs, plans, roadmaps, equivalence_map)

        # Only ONE canonical key present — never the loser id.
        assert set(dlv_map.keys()) == {_WINNER}
        assert dlv_map[_WINNER] == "shipped"

    def test_no_equivalence_map_keeps_legs_independent(self) -> None:
        """Baseline (no declared entry): the two legs stay independent groups — no silent merge."""
        handoffs = [{"deliverable_id": _LOSER, "shipped_sha": "abc123", "deployment_state": "shipped"}]
        plans = [{"deliverable_id": _WINNER, "status": "executing"}]

        dlv_map = deliverable_status._compute_map(handoffs, plans, [], equivalence_map=None)

        assert set(dlv_map.keys()) == {_LOSER, _WINNER}
        assert dlv_map[_LOSER] == "shipped"
        assert dlv_map[_WINNER] == "in-progress"

    def test_unrelated_id_canonicalizes_to_itself(self) -> None:
        """A fork with no declared entry canonicalizes to itself — absence is never a merge."""
        equivalence_map = {_LOSER: _WINNER}
        handoffs = [{"deliverable_id": "dlv-unrelated", "deployment_state": "open"}]

        dlv_map = deliverable_status._compute_map(handoffs, [], [], equivalence_map)

        assert set(dlv_map.keys()) == {"dlv-unrelated"}


class TestStampForkGrouping:
    """stamp() canonicalizes at its own lookup point before writing deliverable_status back."""

    def test_stamp_produces_one_status_across_fork_legs(self, tmp_path: Path) -> None:
        """stamp() with a worktree_root carrying the declared artifact resolves one status."""
        _write_equivalence_artifact(tmp_path)
        _reset_memo()
        try:
            handoffs = [
                {"deliverable_id": _LOSER, "shipped_sha": "abc123", "deployment_state": "shipped"}
            ]
            plans = [{"deliverable_id": _WINNER, "status": "executing"}]
            roadmaps: list[dict] = []

            deliverable_status.stamp(handoffs, plans, roadmaps, worktree_root=tmp_path)

            # Both legs resolve to the SAME (shipped) status — no contradiction.
            assert handoffs[0]["deliverable_status"] == "shipped"
            assert plans[0]["deliverable_status"] == "shipped"
        finally:
            _reset_memo()

    def test_stamp_missing_artifact_degrades_to_raw_comparison(self, tmp_path: Path) -> None:
        """No declared artifact present (C3b not yet landed) — stamp() behaves as before."""
        _reset_memo()
        try:
            handoffs = [
                {"deliverable_id": _LOSER, "shipped_sha": "abc123", "deployment_state": "shipped"}
            ]
            plans = [{"deliverable_id": _WINNER, "status": "executing"}]

            deliverable_status.stamp(handoffs, plans, [], worktree_root=tmp_path)

            assert handoffs[0]["deliverable_status"] == "shipped"
            assert plans[0]["deliverable_status"] == "in-progress"
        finally:
            _reset_memo()

    def test_stamp_does_not_mutate_deliverable_id_field(self, tmp_path: Path) -> None:
        """canonicalize() is a join-key transform only — deliverable_id itself is never rewritten."""
        _write_equivalence_artifact(tmp_path)
        _reset_memo()
        try:
            handoffs = [
                {"deliverable_id": _LOSER, "shipped_sha": "abc123", "deployment_state": "shipped"}
            ]
            deliverable_status.stamp(handoffs, [], [], worktree_root=tmp_path)

            # The record's own deliverable_id is untouched — still the loser id.
            assert handoffs[0]["deliverable_id"] == _LOSER
        finally:
            _reset_memo()

    def test_stamp_idempotent(self, tmp_path: Path) -> None:
        """Running stamp() twice is a no-op the second time (AC12)."""
        _write_equivalence_artifact(tmp_path)
        _reset_memo()
        try:
            handoffs = [
                {"deliverable_id": _LOSER, "shipped_sha": "abc123", "deployment_state": "shipped"}
            ]
            plans = [{"deliverable_id": _WINNER, "status": "executing"}]

            deliverable_status.stamp(handoffs, plans, [], worktree_root=tmp_path)
            first_pass = (handoffs[0]["deliverable_status"], plans[0]["deliverable_status"])

            deliverable_status.stamp(handoffs, plans, [], worktree_root=tmp_path)
            second_pass = (handoffs[0]["deliverable_status"], plans[0]["deliverable_status"])

            assert first_pass == second_pass == ("shipped", "shipped")
        finally:
            _reset_memo()


class TestEnvelopeThreadsWorktreeRootToStamp:
    """Regression: envelope.build()'s real call site must thread ctx.repo_root into stamp().

    Review: code-reviewer (5230f7ac, Finding 1) — stamp()'s sole caller
    (envelope.build()) never passed worktree_root, so the default production emission
    path (repo_root != Path.cwd()) silently loaded an empty equivalence map and left the
    6 named contradictions live in the artifact the cockpit actually reads. This test
    exercises the REAL call path (envelope.build -> _deliverable_status.stamp), with a
    repo_root that deliberately differs from Path.cwd(), and asserts the declared
    equivalence artifact under that root is actually consulted. It fails if the
    ``worktree_root=ctx.repo_root`` argument at envelope.py's call site is reverted.
    """

    def _make_ctx(self, repo_root: Path) -> EmitContext:
        return EmitContext(
            repo_root=repo_root,
            coordinator_root=repo_root,
            central_state_root=repo_root / "state",
            git_branch="test-branch",
            git_sha="0000000000000000000000000000000000000000",
            git_sha_short="00000000",
            observed_at="2026-07-04T00:00:00Z",
            hostname="test-host",
            repo_name="test/repo",
        )

    def _fake_handoffs_plans_sections(self) -> dict:
        """A minimal registry override that injects the fork-split fixture records
        directly, bypassing the real DB-backed collectors (not this test's concern)."""

        def _collect_handoffs(_ctx):
            return (
                [{"deliverable_id": _LOSER, "shipped_sha": "abc123", "deployment_state": "shipped"}],
                [],
            )

        def _place_handoffs(env, records, malformed):
            env["handoffs"] = records
            env["malformed_records"]["handoffs"] = malformed

        def _collect_plans(_ctx):
            return ([{"deliverable_id": _WINNER, "status": "executing"}], [])

        def _place_plans(env, records, malformed):
            env["plans"] = records
            env["malformed_records"]["plans"] = malformed

        def _collect_empty(_ctx):
            return ([], [])

        return {
            "handoffs": _RegisteredSection("handoffs", _collect_handoffs, _place_handoffs),
            "plans": _RegisteredSection("plans", _collect_plans, _place_plans),
        }

    def test_build_threads_repo_root_so_equivalence_artifact_is_consulted(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """repo_root != Path.cwd(); the declared equivalence artifact lives only under
        repo_root, never under cwd. If worktree_root isn't threaded through, stamp()'s
        Path.cwd() fallback finds nothing (this test chdir's to an empty scratch dir
        distinct from repo_root, so there is no accidental real-repo artifact to fall
        back onto) and the two fork legs stay split."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        _write_equivalence_artifact(repo_root)

        cwd_dir = tmp_path / "cwd"
        cwd_dir.mkdir()
        monkeypatch.chdir(cwd_dir)

        _reset_memo()
        assert repo_root != Path.cwd()
        assert not (Path.cwd() / "state" / "deliverable-equivalence.yaml").exists()

        try:
            ctx = self._make_ctx(repo_root)
            envelope = _envelope.build(ctx, sections=self._fake_handoffs_plans_sections())

            # Both legs must resolve to the SAME (shipped) status — proving the
            # equivalence artifact under ctx.repo_root was actually loaded and applied,
            # not silently skipped via a cwd-rooted empty map.
            assert envelope["handoffs"][0]["deliverable_status"] == "shipped"
            assert envelope["plans"][0]["deliverable_status"] == "shipped"
        finally:
            _reset_memo()
