"""coordinator_core.baton_assemble.tests.test_plan_input_predecessor

C6 (2026-08-30, drop-releases-a-claim-it-never-held plan): the plan->execute
seam (`resolve_lineage`'s `is_plan_input` branch) must take the PLAN's own
declared `predecessor_handoff`/`predecessor` field over the session's held
baton -- before this fix, `_resolve_held_handoff_for_session(root)`
unconditionally overwrote the `predecessor` edge even when the plan named its
own predecessor on disk, and even when the held claim shared no lineage with
the plan at all (an unrelated closed baton the session merely happened to be
holding).

Required precedence (this chunk's dispatch brief):
  1. the plan's declared `predecessor_handoff`/`predecessor`, when it
     resolves on disk;
  2. the ledger-held handoff ONLY when it shares lineage with the plan (its
     `governing_plan` names this plan, or it shares the plan's
     `deliverable_id`);
  3. otherwise `none`.

Spec backlink: `coordinator_core/baton_assemble/__init__.py :: resolve_lineage`,
`is_plan_input` branch.

Negative-spec:
    - Leg 2 (the ledger fallback) must NOT be removed -- it is correct for an
      ordinary mid-execution continuation where the session genuinely holds
      this plan's own baton (governing_plan/deliverable_id match).

Run: python3 -m pytest
coordinator_core/baton_assemble/tests/test_plan_input_predecessor.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.test_baton_assemble import (
    _FAKE_OPERATOR_CONFIG,
    _init_repo,
    _write_artifact,
)

# Exercises `brief()` end to end, which shells out to real git for session
# claim resolution -- needs a real repo. Runs at cadence gates, not
# per-commit. Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    """`brief()` calls `resolve_operator_config()` unconditionally -- stubbed
    per `test_j_continuation_vs_fork_excise.py`'s own fixture of the same
    name, absent which real per-machine settings are resolved."""
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
        """Leg 2: the plan declares no predecessor of its own, and the
        session's held baton names THIS plan as its `governing_plan` --
        genuine mid-execution continuation, ledger fallback still fires."""
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
        """Leg 2's other admission gate: no `governing_plan` match, but the
        held baton carries the SAME `deliverable_id` this plan resolved."""
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
        """Leg 3: the plan declares no predecessor of its own, and the
        session's held baton shares neither `governing_plan` nor
        `deliverable_id` with the plan -- this plan's problem statement's
        exact observed defect shape (a plan descending from a sizing object,
        session merely happens to hold an unrelated closed baton). The
        correct edge is `none`, never a guess."""
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
