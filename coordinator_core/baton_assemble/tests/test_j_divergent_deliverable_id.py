"""Tests for `j-divergent-deliverable-id` (2026-08-14, multi-plan sessions
cannot hand off).

Before this point existed, a session whose carry-or-mint rungs disagreed had
no sanctioned route through `baton-assemble`: `resolve_deliverable_and_
initiative` raised `DivergentDeliverableIdError` (correctly -- DR-207 DD#1
refuses an auto-pick), `apply` is the documented sole exit, and the EM was
left hand-authoring the frontmatter the assembler exists to own. That shape
is routine, not an edge case: a `/mise-en-place` autonomous backlog run
ships several plans in one session by design, and `resolve_claimed_plan_path`
names only the earliest-claimed one, so the second plan's id reaching the
predecessor rung is the ordinary multi-plan outcome.

The fix keeps the no-auto-pick invariant whole and removes only the dead
end: the divergence becomes a judgment point with one disposition per
surviving rung, and the operator's choice reaches the SAME `excise_rung` cut
`j-continuation-vs-fork` already threads.

Spec backlink: state/handoffs/2026-08-14-multi-plan-sessions-cannot-hand-off.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field as _read_frontmatter_field
from coordinator_core.session import claims as session_claims
from coordinator_core.session.claimed_plan import resolve_claimed_plan_path
from coordinator_core.test_baton_assemble import (
    _FAKE_OPERATOR_CONFIG,
    _init_repo,
    _write_artifact,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_PLAN_ID = "dlv-plan-side-aaa111"
_PREDECESSOR_ID = "dlv-predecessor-side-bbb222"


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    """`brief()` calls `resolve_operator_config()` unconditionally (B0 seam
    assertion), which resolves real per-machine values absent this stub.
    Restated per-module -- autouse fixtures do not cross module boundaries."""
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))


def _seed_two_plan_session(tmp_path: Path) -> tuple[Path, str, str]:
    """The `/mise-en-place` shape: a session holding TWO claimed plans with
    divergent `deliverable_id`s, handing off against a baton carrying the
    OTHER plan's id. Returns `(predecessor_path, plan_rung_id,
    predecessor_rung_id)`.

    `resolve_claimed_plan_path` names exactly one of the two held claims and
    its tie-break across same-instant claims is not something this fixture
    should encode -- so the predecessor's id is chosen AFTER that resolution,
    as whichever plan did NOT win. The divergence is then deterministic
    without either artifact being malformed, which is the whole point: this
    shape needs no mistake to reach.
    """
    _init_repo(tmp_path)
    plans = {
        "2026-08-14-mise-en-place-first-plan": _PLAN_ID,
        "2026-08-14-mise-en-place-second-plan": _PREDECESSOR_ID,
    }
    for slug, deliverable_id in plans.items():
        _write_artifact(
            tmp_path / "docs" / "plans" / f"{slug}.md",
            [f"deliverable_id: {deliverable_id}"],
        )
        session_claims.claim_plan(slug, cwd=str(tmp_path))

    resolved_plan_rel = resolve_claimed_plan_path(cwd=str(tmp_path))
    assert resolved_plan_rel, "fixture expects the session to hold a plan claim"
    plan_rung_id = _read_frontmatter_field(
        str(tmp_path / resolved_plan_rel), "deliverable_id"
    )
    predecessor_rung_id = next(
        value for value in plans.values() if value != plan_rung_id
    )

    predecessor = _write_artifact(
        tmp_path / "state" / "handoffs" / "2026-08-14-second-plans-baton.md",
        [f"deliverable_id: {predecessor_rung_id}", "initiative: init-second"],
    )
    return predecessor, plan_rung_id, predecessor_rung_id


def _decisions(disposition: str, note: str = "the roadmap stub predates the plan") -> dict:
    return {
        "j-divergent-deliverable-id": {
            "disposition": disposition,
            "decision_note": note,
        }
    }


class TestDivergenceIsActionableNotTerminal:
    def test_multi_plan_session_gets_a_judgment_point_not_a_crash(
        self, tmp_path, monkeypatch
    ):
        """AC1's first half -- the dead end is gone. The two-plan session
        computes a brief instead of raising."""
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-divergence-jp")
        predecessor, plan_rung_id, predecessor_rung_id = _seed_two_plan_session(tmp_path)

        decision = ba.brief(
            "handoff", str(predecessor), repo_root=tmp_path
        ).decision_object

        point = next(
            jp
            for jp in decision["judgment_points"]
            if jp["id"] == "j-divergent-deliverable-id"
        )
        assert {d["value"] for d in point["dispositions"]} == {
            "keep-plan",
            "keep-predecessor",
        }
        assert plan_rung_id in point["evidence"]
        assert predecessor_rung_id in point["evidence"]

    def test_unresolved_divergence_emits_no_directives(self, tmp_path, monkeypatch):
        """The no-auto-pick invariant, structurally: an unresolved point
        leaves nothing for `apply` to fire, so no id can be carried by
        default."""
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-divergence-blocked")
        predecessor, plan_rung_id, predecessor_rung_id = _seed_two_plan_session(tmp_path)

        decision = ba.brief(
            "handoff", str(predecessor), repo_root=tmp_path
        ).decision_object

        assert decision["directives"] == []
        assert not decision["artifact"]["lineage"].get("deliverable_id")

    def test_no_recommendation_is_emitted(self, tmp_path, monkeypatch):
        """DR-207 DD#1's refusal is structural, not conventional:
        `build_untrusted_gate_judgment_point` has no `recommendation`
        parameter at all, so this point cannot nudge either way."""
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-divergence-norec")
        predecessor, plan_rung_id, predecessor_rung_id = _seed_two_plan_session(tmp_path)

        decision = ba.brief(
            "handoff", str(predecessor), repo_root=tmp_path
        ).decision_object

        point = next(
            jp
            for jp in decision["judgment_points"]
            if jp["id"] == "j-divergent-deliverable-id"
        )
        assert point["recommendation"] is None


class TestDispositionsResolveTheCascade:
    def test_keep_predecessor_carries_the_predecessor_rung(self, tmp_path, monkeypatch):
        """AC1's second half -- the sanctioned route completes, and the
        carried id is the one the operator named."""
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-divergence-keep-pred")
        predecessor, plan_rung_id, predecessor_rung_id = _seed_two_plan_session(tmp_path)

        decision = ba.brief(
            "handoff",
            str(predecessor),
            decisions=_decisions("keep-predecessor"),
            repo_root=tmp_path,
        ).decision_object

        assert decision["artifact"]["lineage"]["deliverable_id"] == predecessor_rung_id
        assert decision["directives"]

    def test_keep_plan_carries_the_claimed_plan_rung(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-divergence-keep-plan")
        predecessor, plan_rung_id, predecessor_rung_id = _seed_two_plan_session(tmp_path)

        decision = ba.brief(
            "handoff",
            str(predecessor),
            decisions=_decisions("keep-plan"),
            repo_root=tmp_path,
        ).decision_object

        assert decision["artifact"]["lineage"]["deliverable_id"] == plan_rung_id
        assert decision["directives"]

    def test_decision_note_is_required(self, tmp_path, monkeypatch):
        """The earliest-artifact test is the whole tiebreak, and which
        artifact came first is exactly what cannot be recomputed later from
        the id that was carried -- so an unrecorded reason is refused."""
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-divergence-nonote")
        predecessor, plan_rung_id, predecessor_rung_id = _seed_two_plan_session(tmp_path)

        with pytest.raises(ValueError, match="decision_note"):
            ba.brief(
                "handoff",
                str(predecessor),
                decisions={
                    "j-divergent-deliverable-id": {"disposition": "keep-plan"}
                },
                repo_root=tmp_path,
            )

    def test_unknown_disposition_is_refused(self, tmp_path, monkeypatch):
        """Negative pin: a typo'd or invented disposition fails loud rather
        than degrading to the unresolved arm, which would read as 'the
        engine ignored my choice and blocked anyway'."""
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-divergence-bogus")
        predecessor, plan_rung_id, predecessor_rung_id = _seed_two_plan_session(tmp_path)

        with pytest.raises(ValueError, match="keep-plan"):
            ba.brief(
                "handoff",
                str(predecessor),
                decisions=_decisions("keep-whichever"),
                repo_root=tmp_path,
            )


class TestGuardBranchesRefuseLoudly:
    """The two refusal branches the behavioural tests above do not reach.
    Both exist so a mis-supplied disposition fails loud rather than degrading
    into the unresolved arm, which would read as "the engine ignored my
    choice and blocked anyway"."""

    def test_disposition_on_a_spinoff_is_refused(self, tmp_path, monkeypatch):
        """`j-divergent-deliverable-id` is a handoff-cascade point; the
        spinoff branch resolves its id from the progenitor read, so accepting
        the disposition there would look like it worked while doing nothing."""
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-divergence-spinoff")
        predecessor, _, _ = _seed_two_plan_session(tmp_path)

        with pytest.raises(ValueError, match="handoff-cascade point"):
            ba.brief(
                "spinoff",
                str(predecessor),
                decisions=_decisions("keep-plan"),
                repo_root=tmp_path,
            )

    def test_opposing_excise_rungs_fail_loud(self, tmp_path, monkeypatch):
        """`excise_rung` cuts exactly one rung, so j-continuation-vs-fork and
        j-divergent-deliverable-id naming opposite rungs cannot both be
        honoured -- silently preferring either would pick a carry winner."""
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-divergence-conflict")
        predecessor, _, _ = _seed_two_plan_session(tmp_path)

        with pytest.raises(ValueError, match="opposing rungs"):
            ba.brief(
                "handoff",
                str(predecessor),
                decisions={
                    # keep-plan cuts `predecessor_file`; an explicit
                    # artifact_path makes j-continuation-vs-fork's own excise
                    # cut `plan_file`. Opposite rungs -- the conflict this
                    # guard exists for. (keep-predecessor would AGREE with it
                    # and is correctly not a conflict.)
                    "j-divergent-deliverable-id": {
                        "disposition": "keep-plan",
                        "decision_note": "the claimed plan is earliest",
                    },
                    "j-continuation-vs-fork": {
                        "disposition": "excise",
                        "decision_note": "cutting the predecessor edge",
                    },
                },
                repo_root=tmp_path,
            )


class TestFanInLegsAreNeverFatal:
    """The ordinary N->1 fan-in: two held batons fan into one successor,
    each carrying its own `deliverable_id`. Resolution 1 keeps that shape,
    and `j-fan-in-cardinality` narrates the additional predecessors' ids
    being named-but-not-carried. Feeding those legs to the divergence check
    made that ordinary shape fail outright -- and made j-fan-in-cardinality
    unreachable in exactly the case it describes."""

    @staticmethod
    def _seed_handoff_claim(repo_root: Path, session_id: str, basename: str, claimed_at: str):
        claims_dir = repo_root / ".git" / "coordinator-sessions" / "handoff-claims" / basename
        claims_dir.mkdir(parents=True, exist_ok=True)
        (claims_dir / "session_id").write_text(session_id, encoding="utf-8")
        (claims_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")

    def _seed_fan_in(self, tmp_path: Path, session_id: str) -> str:
        """Two HELD handoff claims -- the fan-in `_resolve_held_handoff_for_
        session` discovers -- whose deliverable_ids differ, plus a claimed
        plan agreeing with the primary. The carrying rungs agree; only the
        fan-in leg dissents."""
        _init_repo(tmp_path)
        plan_slug = "2026-08-14-fan-in-plan"
        _write_artifact(
            tmp_path / "docs" / "plans" / f"{plan_slug}.md",
            [f"deliverable_id: {_PLAN_ID}", "initiative: init-fan-in"],
        )
        primary = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-14-fan-in-primary.md",
            [f"deliverable_id: {_PLAN_ID}", "initiative: init-fan-in"],
        )
        leg = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-14-fan-in-leg.md",
            [f"deliverable_id: {_PREDECESSOR_ID}"],
        )
        self._seed_handoff_claim(tmp_path, session_id, primary.name, "2026-08-14T09:00:00Z")
        self._seed_handoff_claim(tmp_path, session_id, leg.name, "2026-08-14T10:00:00Z")
        session_claims.claim_plan(plan_slug, cwd=str(tmp_path))
        return _PLAN_ID

    def test_dissenting_fan_in_leg_never_blocks_the_brief(self, tmp_path, monkeypatch):
        """No judgment point, no halt: the carrying rungs agree, so there is
        nothing to pick. Runs from the ambient cwd, but that is NOT a
        cwd-bug pin -- fan-in legs never reach `resolve_deliverable_and_
        initiative` at all now, so its cwd-relative `os.path.isfile` probe is
        unreachable from here; not chdir-ing just avoids depending on a cwd
        this test has no reason to control.

        DR-388 (2026-08-30): the agreed carrying-rung id is no longer what
        lands on the successor once a fan-in leg is present -- a fan-in
        successor mints its OWN fresh deliverable_id by construction rather
        than carrying the agreeing rungs' id verbatim. `agreed_id` is still
        asserted into `deliverable_ids[]` (the union), just not onto the
        singular `deliverable_id` field anymore."""
        session_id = "sid-fan-in-nonfatal"
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
        agreed_id = self._seed_fan_in(tmp_path, session_id)

        decision = ba.brief("handoff", "", repo_root=tmp_path).decision_object

        assert not [
            jp for jp in decision["judgment_points"]
            if jp["id"] == "j-divergent-deliverable-id"
        ]
        lineage = decision["artifact"]["lineage"]
        assert lineage["deliverable_id"] != agreed_id, (
            "DR-388: a fan-in successor mints fresh, it does not carry the "
            "agreeing rungs' id verbatim"
        )
        assert agreed_id in (lineage.get("deliverable_ids") or [])
        assert decision["directives"]

    def test_every_leg_survives_in_lineage(self, tmp_path, monkeypatch):
        """Excluded from the divergence CHECK, never from the lineage --
        each fan-in leg still earns its own d6 supersede directive."""
        session_id = "sid-fan-in-lineage"
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
        self._seed_fan_in(tmp_path, session_id)

        decision = ba.brief("handoff", "", repo_root=tmp_path).decision_object

        assert decision["artifact"]["lineage"]["additional_predecessors"], (
            "fan-in leg dropped from lineage, not just from the check"
        )
