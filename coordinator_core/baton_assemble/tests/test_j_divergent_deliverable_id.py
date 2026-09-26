
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

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_PLAN_ID = "dlv-plan-side-aaa111"
_PREDECESSOR_ID = "dlv-predecessor-side-bbb222"


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))


def _seed_two_plan_session(tmp_path: Path) -> tuple[Path, str, str]:
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
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-divergence-blocked")
        predecessor, plan_rung_id, predecessor_rung_id = _seed_two_plan_session(tmp_path)

        decision = ba.brief(
            "handoff", str(predecessor), repo_root=tmp_path
        ).decision_object

        assert decision["directives"] == []
        assert not decision["artifact"]["lineage"].get("deliverable_id")

    def test_no_recommendation_is_emitted(self, tmp_path, monkeypatch):
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

    def test_disposition_on_a_spinoff_is_refused(self, tmp_path, monkeypatch):
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
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-divergence-conflict")
        predecessor, _, _ = _seed_two_plan_session(tmp_path)

        with pytest.raises(ValueError, match="opposing rungs"):
            ba.brief(
                "handoff",
                str(predecessor),
                decisions={
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

    @staticmethod
    def _seed_handoff_claim(repo_root: Path, session_id: str, basename: str, claimed_at: str):
        claims_dir = repo_root / ".git" / "coordinator-sessions" / "handoff-claims" / basename
        claims_dir.mkdir(parents=True, exist_ok=True)
        (claims_dir / "session_id").write_text(session_id, encoding="utf-8")
        (claims_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")

    def _seed_fan_in(self, tmp_path: Path, session_id: str) -> str:
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
        session_id = "sid-fan-in-lineage"
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
        self._seed_fan_in(tmp_path, session_id)

        decision = ba.brief("handoff", "", repo_root=tmp_path).decision_object

        assert decision["artifact"]["lineage"]["additional_predecessors"], (
            "fan-in leg dropped from lineage, not just from the check"
        )
