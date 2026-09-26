"""
coordinator_core.reconcile.tests.test_gate_eval_gate_notes_alias — IBMFR-R18.

`_has_blocking_notes` read only the deprecated `blocking_notes` key. A baton
gated the current-schema way (handoff schema 8.11.0, DR-190 §13) carries
`gate_notes` instead — the same alias order `schema_validate.py`'s
`_cf_awaiting_gate_needs_dependency` already applies. Mirrors
`test_gate_eval.py::TestBlockingNotesDominatesVacuousEmptyBlockedBy`, with
`gate_notes` in place of `blocking_notes`: a non-empty `gate_notes` on an
otherwise-vacuous empty `blocked_by` must still dominate to `surface`, never
vacuously `clear`.
"""

from __future__ import annotations

from coordinator_core.reconcile.gate_eval import evaluate_gate


class TestGateNotesAliasDominatesVacuousEmptyBlockedBy:

    def test_empty_blocked_by_with_gate_notes_does_not_vacuously_clear(self) -> None:
        handoff = {
            "id": "hnd-gate-notes-alias-000001",
            "handoff_id": "hnd-gate-notes-alias-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "gate_notes": (
                "Windows machine required for AC7 verification — no baton, "
                "advisory only"
            ),
        }

        result = evaluate_gate(handoff, [handoff])

        assert result["verdict"] != "clear"
        assert result["verdict"] == "surface"

    def test_whitespace_only_gate_notes_is_empty_still_vacuously_clears(self) -> None:
        handoff = {
            "id": "hnd-gate-notes-alias-000002",
            "handoff_id": "hnd-gate-notes-alias-000002",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "gate_notes": "   ",
        }

        result = evaluate_gate(handoff, [handoff])

        assert result["verdict"] == "clear"

    def test_blank_gate_notes_falls_back_to_blocking_notes(self) -> None:
        """`gate_notes` absent/empty must still fall back to the deprecated
        `blocking_notes` — the alias is additive, not a replacement."""
        handoff = {
            "id": "hnd-gate-notes-alias-000003",
            "handoff_id": "hnd-gate-notes-alias-000003",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "blocking_notes": "Windows machine required for AC7 verification",
        }

        result = evaluate_gate(handoff, [handoff])

        assert result["verdict"] == "surface"
