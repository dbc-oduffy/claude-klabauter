"""
Regression tests for IBMFR-R04 (docs/plans/2026-09-26-inbox-blitz-claude-klabauter-
fixes-fyi-rest.md § R04; state/cross-repo/archive/2026-09-12-market-
intelligence-em-unclaim-refuses-awaiting-gate-and-created-paths-have-no-
claimant.md, Gap 1).

Gap 1 pin: ``reconcile_dead_handoff_claim_frontmatter`` used to pre-check
``deployment_state`` and SKIP the frontmatter reconcile step (leaving the
handoff misreporting a dead session as its claimant forever) for any
``deployment_state`` outside ``{in_flight, ready_to_fire}`` -- ``awaiting_gate``
included. The precondition is removed: the function now always attempts
the reconcile via ``_unclaim``, which already fails closed (and reports,
never raises) for a genuinely out-of-scope state via its own
``locked_rmw``/``MutateAbort`` handling. This suite pins that an
``awaiting_gate`` baton's stale claimant clears just like an ``in_flight``
one.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.session import claims

from .test_claims import _make_claim, _make_repo, _write_handoff


def _status_of(hpath: Path) -> str | None:
    for line in hpath.read_text(encoding="utf-8").splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    return None


def _deployment_state_of(hpath: Path) -> str | None:
    for line in hpath.read_text(encoding="utf-8").splitlines():
        if line.startswith("deployment_state:"):
            return line.split(":", 1)[1].strip()
    return None


def _claimed_by_of(hpath: Path) -> str | None:
    for line in hpath.read_text(encoding="utf-8").splitlines():
        if line.startswith("claimed_by:"):
            return line.split(":", 1)[1].strip()
    return None


class TestReconcileClearsAnAwaitingGateStaleClaimant:
    """Gap 1: an ``awaiting_gate`` baton's dead claimant now clears instead
    of being permanently stranded — no ``deployment_state`` precondition on
    the frontmatter-reconcile step of the dead-claimant path."""

    @staticmethod
    def _sessions_dir(repo):
        return Path(repo) / ".git" / "coordinator-sessions"

    def test_awaiting_gate_stale_claimant_clears(self, tmp_path):
        repo = _make_repo(tmp_path)
        hpath = _write_handoff(
            repo, "pred.md", status="claimed", deployment_state="awaiting_gate"
        )
        _make_claim(
            repo,
            "handoff",
            "pred.md",
            session_id="11111111-1111-4111-8111-111111111111",
            pid="123",
        )
        claims.reconcile_dead_handoff_claim_frontmatter(
            "pred.md", self._sessions_dir(repo)
        )
        # The dead claimant is cleared -- but deployment_state is not
        # `in_flight`/`ready_to_fire`, so this routes through the
        # claimant-only clear, not `_unclaim`: `status` and
        # `deployment_state` are left exactly as they were.
        assert _claimed_by_of(hpath) is None
        assert _status_of(hpath) == "claimed"

    def test_awaiting_gate_reconcile_leaves_deployment_state_untouched(
        self, tmp_path
    ):
        """The claimant-only clear never routes an ``awaiting_gate`` baton
        through ``_unclaim`` -- ``deployment_state`` stays exactly
        ``awaiting_gate`` (this is what distinguishes the fix from
        widening ``_unclaim``'s own precondition, which stays out of this
        chunk's writes scope)."""
        repo = _make_repo(tmp_path)
        hpath = _write_handoff(
            repo, "pred.md", status="claimed", deployment_state="awaiting_gate"
        )
        _make_claim(
            repo,
            "handoff",
            "pred.md",
            session_id="11111111-1111-4111-8111-111111111111",
            pid="123",
        )
        claims.reconcile_dead_handoff_claim_frontmatter(
            "pred.md", self._sessions_dir(repo)
        )
        assert _deployment_state_of(hpath) == "awaiting_gate"

    def test_still_reconciles_the_previously_accepted_in_flight_state(
        self, tmp_path
    ):
        """No regression on the shape that already worked."""
        repo = _make_repo(tmp_path)
        hpath = _write_handoff(
            repo, "pred.md", status="claimed", deployment_state="in_flight"
        )
        _make_claim(
            repo,
            "handoff",
            "pred.md",
            session_id="11111111-1111-4111-8111-111111111111",
            pid="123",
        )
        claims.reconcile_dead_handoff_claim_frontmatter(
            "pred.md", self._sessions_dir(repo)
        )
        assert _status_of(hpath) == "open"
        assert _deployment_state_of(hpath) == "ready_to_fire"

    def test_a_terminal_deployment_state_is_still_left_alone(self, tmp_path):
        """Removing the precondition never routes a terminal record (e.g.
        ``shipped``) back through unclaim -- ``_unclaim`` itself still
        refuses that (C1-Q1 ruling, handoff-legal-state-table.md § Q1), and
        that refusal is reported, not raised: ``status`` stays untouched."""
        repo = _make_repo(tmp_path)
        hpath = _write_handoff(
            repo, "pred.md", status="claimed", deployment_state="shipped"
        )
        _make_claim(
            repo,
            "handoff",
            "pred.md",
            session_id="11111111-1111-4111-8111-111111111111",
            pid="123",
        )
        claims.reconcile_dead_handoff_claim_frontmatter(
            "pred.md", self._sessions_dir(repo)
        )
        assert _status_of(hpath) == "claimed"
        assert _deployment_state_of(hpath) == "shipped"
