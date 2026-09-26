from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.test_baton_assemble import (
    _FAKE_OPERATOR_CONFIG,
    _init_repo,
    _write_artifact,
)

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))


def _seed_handoff_claim(repo_root: Path, session_id: str, basename: str) -> None:
    claims_dir = repo_root / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    claims_dir.mkdir(parents=True, exist_ok=True)
    (claims_dir / "session_id").write_text(session_id, encoding="utf-8")


def _write_plan_input(
    repo_root: Path,
    plan_rel: str,
    *,
    deliverable_id: str = "dlv-plan-input-000",
    predecessor: str | None = None,
) -> Path:
    lines = [f"plan_id: {Path(plan_rel).stem}", f"deliverable_id: {deliverable_id}"]
    if predecessor is not None:
        lines.append(f"predecessor_handoff: {predecessor}")
    return _write_artifact(repo_root / plan_rel, lines)


class TestDeclaredPredecessorWinsOverLedger:
    def test_ac1_plans_own_declared_predecessor_wins(self, tmp_path, monkeypatch):
        """Leg 1: the plan names its own predecessor and it resolves on
        disk -- that edge wins even though the session holds an UNRELATED
        handoff claim (the observed defect's exact shape: a closed, unrelated
        baton the session merely happens to be holding)."""
        _init_repo(tmp_path)
        session_id = "sid-plan-input-declared-wins"
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        declared_predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-30-declared-predecessor.md",
            ["handoff_id: hnd-declared-1a2b3c"],
        )
        unrelated_held = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-29-unrelated-closed.md",
            ["kind: session-handoff", "handoff_id: hnd-unrelated-9z8y7x"],
        )
        _seed_handoff_claim(tmp_path, session_id, unrelated_held.name)

        plan = _write_plan_input(
            tmp_path,
            "docs/plans/2026-08-30-plan-input-declared.md",
            predecessor=str(declared_predecessor.relative_to(tmp_path)),
        )

        decision = ba.brief("handoff", str(plan), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["predecessor"] == str(
            declared_predecessor.relative_to(tmp_path)
        ).replace("\\", "/")
        assert lineage["predecessor_id"] == "hnd-declared-1a2b3c"


class TestLedgerFallbackGatedOnSharedLineage:
    def test_ac2_ledger_held_handoff_used_when_it_shares_governing_plan(
        self, tmp_path, monkeypatch
    ):
        _init_repo(tmp_path)
        session_id = "sid-plan-input-ledger-shares-plan"
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        plan_rel = "docs/plans/2026-08-30-plan-input-shared.md"
        plan = _write_plan_input(tmp_path, plan_rel)

        held = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-30-held-continuation.md",
            [
                "kind: session-handoff",
                "handoff_id: hnd-held-continuation-1a2b3c",
                f"governing_plan: {plan_rel}",
            ],
        )
        _seed_handoff_claim(tmp_path, session_id, held.name)

        decision = ba.brief("handoff", str(plan), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["predecessor"] == str(held.relative_to(tmp_path)).replace("\\", "/")
        assert lineage["predecessor_id"] == "hnd-held-continuation-1a2b3c"

    def test_ac3_ledger_held_handoff_used_when_it_shares_deliverable_id(
        self, tmp_path, monkeypatch
    ):
        _init_repo(tmp_path)
        session_id = "sid-plan-input-ledger-shares-deliverable"
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        plan = _write_plan_input(
            tmp_path,
            "docs/plans/2026-08-30-plan-input-shared-deliverable.md",
            deliverable_id="dlv-shared-abc123",
        )
        held = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-30-held-shared-deliverable.md",
            [
                "kind: session-handoff",
                "handoff_id: hnd-held-shared-4d5e6f",
                "deliverable_id: dlv-shared-abc123",
            ],
        )
        _seed_handoff_claim(tmp_path, session_id, held.name)

        decision = ba.brief("handoff", str(plan), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["predecessor"] == str(held.relative_to(tmp_path)).replace("\\", "/")
        assert lineage["predecessor_id"] == "hnd-held-shared-4d5e6f"

    def test_ac4_unrelated_ledger_claim_is_declined_not_guessed(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        session_id = "sid-plan-input-ledger-unrelated"
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        plan = _write_plan_input(
            tmp_path,
            "docs/plans/2026-08-30-plan-input-unrelated.md",
            deliverable_id="dlv-plan-own-000",
        )
        unrelated_held = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-29-rebuild-eol-repair-under-the-bar.md",
            [
                "kind: session-handoff",
                "handoff_id: hnd-unrelated-9z8y7x",
                "deliverable_id: dlv-totally-unrelated-999",
            ],
        )
        _seed_handoff_claim(tmp_path, session_id, unrelated_held.name)

        decision = ba.brief("handoff", str(plan), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["predecessor"] is None
        assert lineage["predecessor_id"] is None
