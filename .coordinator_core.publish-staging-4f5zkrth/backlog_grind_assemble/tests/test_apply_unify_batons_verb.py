"""
coordinator_core.backlog_grind_assemble.tests.test_apply_unify_batons_verb

Purpose: pins AC10's mutation half at the ASSEMBLER seam
(docs/plans/2026-08-19-batons-unify-into-one-successor.md, AC10) — the
`unify-batons` verb `readers_mise._unify_batons_directive` names, its
presence in the CLOSED `_CLI_DISPATCH` table, the `args[0]` repack that
carries the reader's sibling keys across `apply_base`'s handler seam, and
the delegation to C5's routed path.

Three properties this file exists to keep true:

  - The reader's directive RESOLVES. `apply_base.resolve_cli` pre-validates
    every directive's `cli` before any directive in the run executes, so a
    verb missing from the table does not degrade to inert decoration — it
    raises and takes down the whole `/mise-en-place` apply run. An earlier
    shape of this reader shipped a `cli`-less directive and would have done
    exactly that.
  - The handler DELEGATES and never re-implements. The plan's anti-scope:
    unification has exactly one implementation (C5's routed path), and a
    second dispatch verb that re-implements it is the failure being
    avoided.
  - The legs cross the seam. `execute_directives` passes ONLY `args` and
    `repo_root` to a handler, so the reader's sibling keys reach it solely
    through `_prepare_directives_for_dispatch`'s repack.

No git, no subprocess: the routed path is stubbed at
`coordinator_core.pickup_assemble.unify_run_batons`, whose own behaviour is
proven in `pickup_assemble/tests/test_baton_unification.py`.

Run: cd X:/project-makima && python -m pytest
coordinator_core/backlog_grind_assemble/tests/test_apply_unify_batons_verb.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import coordinator_core.pickup_assemble as pa
from coordinator_core.backlog_grind_assemble import apply as bga_apply
from coordinator_core.backlog_grind_assemble import readers_mise
from coordinator_core.contract import apply_base


def _reader_directive(legs: list[str], *, fallback: int = 0) -> dict:
    return readers_mise._unify_batons_directive(
        readers_mise._BatonInheritance(tuple(legs), fallback),
        "state/mise-inventory/run-1.md",
    )


def test_the_readers_verb_resolves_through_the_closed_dispatch_table():
    directive = _reader_directive(["state/handoffs/a.md"])
    assert directive["cli"] in bga_apply._CLI_DISPATCH
    # The pre-validation pass the whole run dies on — exercised directly.
    assert (
        apply_base.resolve_cli(bga_apply._CLI_DISPATCH, directive["cli"])
        is bga_apply._dispatch_unify_batons
    )


def test_repack_carries_legs_and_sibling_keys_into_args_zero():
    directive = _reader_directive(
        ["state/handoffs/a.md", "state/handoffs/b.md"], fallback=1
    )
    prepared = bga_apply._prepare_directives_for_dispatch([directive])
    assert len(prepared) == 1
    payload = json.loads(prepared[0]["args"][0])
    assert payload == {
        "legs": ["state/handoffs/a.md", "state/handoffs/b.md"],
        "role_axis_fallback_count": 1,
        "inventory_record": "state/mise-inventory/run-1.md",
    }


def test_handler_delegates_to_the_routed_path_and_reports_its_result(monkeypatch):
    seen: list[tuple] = []

    def _fake_unify(root, legs):
        seen.append((root, list(legs)))
        return {
            "unified": True,
            "run_legs": list(legs),
            "parents": list(legs),
            "successor": "state/handoffs/successor.md",
            "reason": "handover",
        }

    monkeypatch.setattr(pa, "unify_run_batons", _fake_unify)

    directive = _reader_directive(["state/handoffs/a.md"], fallback=2)
    prepared = bga_apply._prepare_directives_for_dispatch([directive])[0]
    repo_root = Path("X:/nonexistent-repo")

    report = bga_apply._CLI_DISPATCH[directive["cli"]](prepared["args"], repo_root)

    assert seen == [(repo_root, ["state/handoffs/a.md"])]
    assert report["unified"] is True
    assert report["successor"] == "state/handoffs/successor.md"
    # The reader's own counted fallback survives the round trip — AC10 gates
    # retirement of the path-shape heuristic on it reaching zero, which is
    # unreadable if the verb drops it.
    assert report["role_axis_fallback_count"] == 2
    assert report["inventory_record"] == "state/mise-inventory/run-1.md"


def test_handler_does_not_swallow_a_half_moved_tree(monkeypatch):
    """A raise out of the routed path means a mint or a parent stamp failed
    with the tree half-moved. The run must see it — the same reason C5
    refuses to wrap `_unify_into_successor` in a blanket `except`."""

    def _boom(_root, _legs):
        raise RuntimeError("mint failed after parents stamped")

    monkeypatch.setattr(pa, "unify_run_batons", _boom)

    prepared = bga_apply._prepare_directives_for_dispatch(
        [_reader_directive(["state/handoffs/a.md"])]
    )[0]

    try:
        bga_apply._CLI_DISPATCH["unify-batons"](prepared["args"], Path("X:/nope"))
    except RuntimeError as exc:
        assert "mint failed" in str(exc)
    else:  # pragma: no cover - the assertion this test exists for
        raise AssertionError("handler swallowed a half-moved-tree failure")


def test_predicate_off_keeps_the_verb_a_reporting_no_op(monkeypatch):
    """End to end through the real routed path, predicate at its shipped
    default: the verb resolves, dispatches, and mutates nothing — the
    property that lets this land before the flip commit."""
    monkeypatch.setattr(pa, "_baton_unification_routing_enabled", lambda: False)

    prepared = bga_apply._prepare_directives_for_dispatch(
        [_reader_directive(["state/handoffs/a.md"])]
    )[0]
    report = bga_apply._CLI_DISPATCH["unify-batons"](prepared["args"], Path("X:/nope"))

    assert report["unified"] is False
    assert report["reason"] == "routing-disabled"
    assert report["run_legs"] == ["state/handoffs/a.md"]
