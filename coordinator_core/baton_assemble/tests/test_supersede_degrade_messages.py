"""
coordinator_core.baton_assemble.tests.test_supersede_degrade_messages —
P026-C1, AC3.

Pins the message-text half of AC3
(docs/plans/2026-09-07-a-claim-is-written-twice-and-nothing-compares-them.md):
`_dispatch_handoff_supersede_predecessor` never presents
`handoff.supersede_predecessor` itself as an invocable op in its message
text, and every degrade branch whose advice is "re-run" names
`handoff.transition` verb=supersede as the runnable route, stating the
predecessor must be claimed first.

Two branches carry re-run advice and are asserted here:
  - the unresolved fan-in placeholder refusal
  - the `handoff.housekeeping` suspension ("housekeeping-off") refusal

The `predecessor-not-claimed-or-shipped` (choke-point) branch already
carries the claim-first clause per AC3's own text ("checked, not
rewritten") and is intentionally not asserted here — its message is the
op's own to word, not this wrapper's.

The structured, unchanged-by-this-AC half (the `cli` field, `_DISPATCH`
registration, `NOT REGISTERED -- stays cli-named` label) is pinned
separately; this file is message-text only.

Spec backlink: docs/plans/2026-09-07-a-claim-is-written-twice-and-nothing-
compares-them.md, AC3 / P026-C1.

Run (from repo root):
    python3 -m pytest coordinator_core/baton_assemble/tests/test_supersede_degrade_messages.py -q
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.baton_assemble import apply as ba_apply

_PRED_REL = "state/handoffs/predecessor.md"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_placeholder_refusal_names_handoff_transition_supersede_and_claim_first(
    tmp_path, capsys
):
    successor_rel = "state/handoffs/successor-fan-in.md"
    _write(
        tmp_path / successor_rel,
        (
            "---\n"
            'title: "PLACEHOLDER --- replace with one-line handoff title"\n'
            "additional_predecessors:\n"
            "  - state/handoffs/other-predecessor.md\n"
            "---\n\n# Successor\n"
        ),
    )
    _write(tmp_path / _PRED_REL, "---\ntitle: predecessor\n---\n\n# Predecessor\n")

    result = ba_apply._dispatch_handoff_supersede_predecessor(
        [_PRED_REL, successor_rel, successor_rel], tmp_path
    )

    assert result["degraded"]["reason"] == "continued-into-target-is-placeholder"
    stderr = capsys.readouterr().err
    assert "handoff.transition" in stderr
    assert "verb=supersede" in stderr
    assert "claimed first" in stderr
    assert "run `handoff.supersede_predecessor`" not in stderr
    assert "invoke `handoff.supersede_predecessor`" not in stderr


def test_housekeeping_off_refusal_names_handoff_transition_supersede_and_claim_first(
    tmp_path, monkeypatch, capsys
):
    from coordinator_core.op_budget_suspension import OpSuspendedError
    from coordinator_core.test_baton_assemble import _PREDECESSOR_FM, _write_artifact

    successor_rel = "state/handoffs/successor-housekeeping-off.md"
    _write(tmp_path / successor_rel, "---\ntitle: successor\n---\n\n# Successor\n")
    _write_artifact(tmp_path / _PRED_REL, list(_PREDECESSOR_FM))

    def _suspended(op_name, params, repo_root):
        raise OpSuspendedError(f"{op_name} is off: p50 250ms against a 200ms bar")

    monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _suspended)

    result = ba_apply._dispatch_handoff_supersede_predecessor(
        [_PRED_REL, successor_rel, successor_rel], tmp_path
    )

    assert result["degraded"]["reason"] == "housekeeping-off"
    stderr = capsys.readouterr().err
    assert "handoff.transition" in stderr
    assert "verb=supersede" in stderr
    assert "claimed first" in stderr
    assert "run `handoff.supersede_predecessor`" not in stderr
    assert "invoke `handoff.supersede_predecessor`" not in stderr


def test_dispatch_table_and_cli_field_stay_unchanged_by_this_ac():
    from coordinator_core.baton_assemble import apply as ba_apply_mod

    assert "handoff.supersede_predecessor" in ba_apply_mod._CLI_DISPATCH
