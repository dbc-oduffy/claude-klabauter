"""C6 / AC3: the transition op derives readiness instead of carrying a stale one.

docs/plans/2026-08-19-gate-notes-are-advisory-blocked-by-derives-readiness.md § C6.

AC3 is "clearing a gate flips readiness back without a hand edit". Authoring
alone cannot deliver it — the gate clears LATER, on a mutating path, not at
birth — so `_apply_derived_readiness` is that mutating-path call site and this
file is its pin. The chunk shipped its implementation without this test surface;
until it existed, AC3 was implemented but unverified.

NEGATIVE-SPEC exercised here, not merely described:
  - `blocking_notes` derives nothing. A record carrying only a gate note stays
    pickup-ready (the plan's § Anti-scope "single most likely wrong turn").
  - Off-gate-axis lifecycle states are never touched.
  - `review-due` is never auto-promoted.
"""

from pathlib import Path

import pytest

from coordinator_core.ops.handoff_transition import _apply_derived_readiness


def _fm_text(**fields) -> str:
    """Frontmatter BODY text (no `---` fences) — what the helper is handed."""
    lines = []
    for key, value in fields.items():
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  - {item}" for item in value)
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines) + "\n"


def _blocker(handoff_id: str, deployment_state: str) -> dict:
    return {
        "handoff_id": handoff_id,
        "id": handoff_id,
        "kind": "session-handoff",
        "deployment_state": deployment_state,
        "shipped_in": "a" * 40 if deployment_state == "shipped" else None,
    }


@pytest.fixture
def corpus(monkeypatch):
    """Pin the gate index the helper walks, so no test reads live state/."""

    def _install(records):
        import coordinator_core.ops.handoff_reconcile as hr

        monkeypatch.setattr(
            hr,
            "_collect_all_handoffs_for_gate_index",
            lambda _worktree: (records, []),
        )

    return _install


class TestClearingAGateFlipsReadinessBack:
    """AC3 proper — the criterion the whole chunk exists for."""

    def test_shipped_blocker_does_not_free_while_blocked_by_still_names_it(self, corpus):
        """TIGHTEN-ONLY. Even with the blocker shipped, `ready_to_fire` cannot
        be written while `blocked_by` still names it — the schema holds a
        union invariant across `blocked_by`/`no_longer_blocked_by` (a resolved
        blocker MOVES, never vanishes) and the cross-field rule refuses the
        combination outright. `gate_cascade_clear` owns that move; this seam
        declines rather than writing a record the validator rejects."""
        corpus([_blocker("hnd-blocker-000001", "shipped")])
        fm = _fm_text(
            deployment_state="awaiting_gate",
            pickup_ready=False,
            blocked_by=["hnd-blocker-000001"],
        )

        result = _apply_derived_readiness(fm, Path("."))

        assert "deployment_state: awaiting_gate" in result
        assert "pickup_ready: false" in result
        assert "hnd-blocker-000001" in result

    def test_drained_blocked_by_frees_the_baton(self, corpus):
        """The freed direction it CAN legally take: nothing left in
        `blocked_by`, so no move is owed and `ready_to_fire` is honest."""
        corpus([])
        fm = _fm_text(
            deployment_state="awaiting_gate",
            pickup_ready=False,
            blocked_by=[],
        )

        result = _apply_derived_readiness(fm, Path("."))

        assert "deployment_state: ready_to_fire" in result
        assert "pickup_ready: true" in result

    def test_blocker_still_open_keeps_the_baton_parked(self, corpus):
        corpus([_blocker("hnd-blocker-000001", "awaiting_gate")])
        fm = _fm_text(
            deployment_state="ready_to_fire",
            pickup_ready=True,
            blocked_by=["hnd-blocker-000001"],
        )

        result = _apply_derived_readiness(fm, Path("."))

        assert "deployment_state: awaiting_gate" in result
        assert "pickup_ready: false" in result

    @pytest.mark.parametrize("state", ["continued", "closed"])
    def test_terminal_but_not_shipped_blocker_does_not_free_the_baton(self, corpus, state):
        """The claude-klabauter-d3 correction, pinned at THIS call site too:
        FREED is strictly `shipped`; continued/closed are terminal but not done."""
        corpus([_blocker("hnd-blocker-000001", state)])
        fm = _fm_text(
            deployment_state="awaiting_gate",
            pickup_ready=False,
            blocked_by=["hnd-blocker-000001"],
        )

        result = _apply_derived_readiness(fm, Path("."))

        assert "deployment_state: awaiting_gate" in result
        assert "pickup_ready: false" in result

    def test_pickup_ready_is_inserted_when_absent(self, corpus):
        """The seed scaffolds omit `pickup_ready`; when the derivation does have
        a verdict, the field is inserted after `deployment_state` rather than
        silently dropped."""
        corpus([])
        fm = _fm_text(
            deployment_state="awaiting_gate",
            blocked_by=[],
        )

        result = _apply_derived_readiness(fm, Path("."))

        assert "pickup_ready: true" in result


class TestAc3ThroughTheLiveReparkVerb:
    """AC3 on a REAL mutating path, not just the helper.

    Review (code-reviewer slice B, critical): the derivation's only wired call
    site was `_claim`, which stamps `in_flight` — off the gate axis — BEFORE
    calling it, so it returned (None, None) there every single time. The
    commit's own comment said "No-op here in practice". AC3 was therefore
    proven only by calling the helper directly, which proves the helper works
    and not that any live transition uses it. `repark` is the verb that
    actually leaves a handoff on the gate axis.
    """

    @pytest.fixture(autouse=True)
    def _git_dir(self, tmp_path, monkeypatch):
        """`locked_rmw` resolves a lock sidecar under the git common dir.
        Stub the seam rather than `git init` the fixture: a real init would
        spawn a process, which puts the test behind the spawn ratchet's
        `spawns_process`/`cadence` markers and off the fast tier — for a
        dependency this test does not actually care about.
        """
        gitdir = tmp_path / ".git"
        gitdir.mkdir(exist_ok=True)
        monkeypatch.setattr(
            "coordinator_core.git.repo_root.git_common_dir", lambda _root: str(gitdir)
        )
        import coordinator_core.lifecycle as _lc

        if hasattr(_lc, "_git_common_dir_cached"):
            _lc._git_common_dir_cached.cache_clear()

    def _handoff(self, tmp_path, blocked_by):
        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True)
        p = d / "2026-08-19-parked.md"
        blockers = "\n".join(f"  - {b}" for b in blocked_by)
        p.write_text(
            "---\n"
            "title: \"Parked baton\"\n"
            "created: 2026-08-19\n"
            "branch: \"work/test/2026-08-19\"\n"
            "status: claimed\n"
            "predecessor: none\n"
            "kind: session-handoff\n"
            "deployment_state: in_flight\n"
            "category: infra\n"
            "summary: \"A parked baton used to pin AC3 through repark\"\n"
            "pickup_ready: false\n"
            + (f"blocked_by:\n{blockers}\n" if blocked_by else "blocked_by: []\n")
            + "---\n\n## What Was Accomplished\n\nbody\n",
            encoding="utf-8",
        )
        return p

    def test_repark_does_not_free_a_baton_whose_blocked_by_is_still_populated(
        self, tmp_path, corpus
    ):
        """TIGHTEN-ONLY, and this test is why it exists rather than being a
        preference. Even with every blocker shipped, writing `ready_to_fire`
        while `blocked_by` still names them produces a record the cross-field
        rule rejects, and the whole repark aborts. Freeing requires MOVING the
        ids into `no_longer_blocked_by`, which `gate_cascade_clear` owns.

        So repark must leave such a record alone rather than abort — the
        transition still succeeds, it simply declines to make a claim it
        cannot legally write.
        """
        from coordinator_core.ops.handoff_transition import _repark

        corpus([_blocker("hnd-blocker-000001", "shipped")])
        p = self._handoff(tmp_path, ["hnd-blocker-000001"])

        result = _repark(str(p), tmp_path, tmp_path)

        assert result.get("exit_code", 0) == 0, result
        text = p.read_text(encoding="utf-8")
        assert "hnd-blocker-000001" in text, "the blocker must not be silently dropped"
        assert "deployment_state: awaiting_gate" in text
        assert "pickup_ready: false" in text

    def test_repark_frees_a_baton_with_no_blockers_left(self, tmp_path, corpus):
        """The freed direction the derivation CAN legally take: nothing in
        `blocked_by`, so no MOVE is owed and `ready_to_fire` is honest."""
        from coordinator_core.ops.handoff_transition import _repark

        corpus([])
        p = self._handoff(tmp_path, [])

        result = _repark(str(p), tmp_path, tmp_path)

        assert result.get("exit_code", 0) == 0, result
        text = p.read_text(encoding="utf-8")
        assert "deployment_state: ready_to_fire" in text
        assert "pickup_ready: true" in text

    def test_repark_keeps_the_baton_parked_while_its_blocker_is_open(self, tmp_path, corpus):
        """The half that matters most: repark hardcoded ready_to_fire, an
        assertion it cannot make. A baton handed back with an unresolved
        blocker is not ready to fire, and stamping it so is the exact lie
        this plan exists to stop."""
        from coordinator_core.ops.handoff_transition import _repark

        corpus([_blocker("hnd-blocker-000001", "awaiting_gate")])
        p = self._handoff(tmp_path, ["hnd-blocker-000001"])

        result = _repark(str(p), tmp_path, tmp_path)

        assert result.get("exit_code", 0) == 0, result
        text = p.read_text(encoding="utf-8")
        assert "deployment_state: awaiting_gate" in text
        assert "pickup_ready: false" in text


class TestDerivationHasNoOpinionAndLeavesTheRecordAlone:
    """The `(None, None)` returns are what make the C6 wiring safe to call
    unconditionally after every verb's own stamp — zero enum-narrowing risk."""

    @pytest.mark.parametrize("state", ["in_flight", "shipped", "continued", "closed"])
    def test_off_gate_axis_states_are_returned_untouched(self, corpus, state):
        corpus([])
        fm = _fm_text(deployment_state=state, pickup_ready=False, blocked_by=[])

        assert _apply_derived_readiness(fm, Path(".")) == fm


class TestGateNotesDeriveNothingAtThisCallSite:
    """§ Anti-scope: the single most likely wrong turn in the plan. A record
    carrying only a gate note has an EMPTY blocked_by and must stay pickup-ready
    — nothing can ever clear an inert prose field, so gating on it would park
    the baton permanently."""

    def test_gate_note_alone_does_not_park_the_baton(self, corpus):
        corpus([])
        fm = _fm_text(
            deployment_state="ready_to_fire",
            pickup_ready=True,
            blocked_by=[],
            blocking_notes="needs a box with hefty GPU/VRAM",
        )

        result = _apply_derived_readiness(fm, Path("."))

        assert "deployment_state: ready_to_fire" in result
        assert "pickup_ready: true" in result
        assert "blocking_notes: needs a box with hefty GPU/VRAM" in result
