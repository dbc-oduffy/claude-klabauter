"""Boundary pins for `coordinator_core/execute_plan_assemble/apply.py` (C2).

Two properties invisible until they break:

1. The emit leg goes through the DoE `emit-dispatch-workflow.py` SCRIPT,
   never the native `dispatch.emit` op. A static AST scan over `apply.py`
   asserting it neither imports nor calls
   `coordinator_core.ops.dispatch_emit.emit.emit_script` / the `dispatch.emit`
   op — this is what keeps DoE-claude's auto-fired
   `block-workflow-foreign-emission.py` able to do its job: the receipt it
   compares against is written by the DoE script's own stamping leg, and a
   script emitted around that leg is precisely a foreign emission.

2. The same-ceremony boundary: `_CLI_DISPATCH`'s key set is EXACTLY the four
   Phase-1 verbs, and in particular contains neither `mint-deliverable-id`
   nor `advance-tracker-status` — both belong to other ceremonies
   (`roadmap-planning`, `enrich-and-review`), not `/execute-plan`'s own
   SKILL.md, and firing either from here would fire another ceremony's step
   out of its own ceremony.

Spec: docs/plans/2026-09-11-the-execute-plan-pre-execution-chain-emi.md, C3
"""
from __future__ import annotations

import ast
from pathlib import Path

from coordinator_core.execute_plan_assemble.apply import _CLI_DISPATCH

_APPLY_PATH = Path(__file__).resolve().parents[1] / "apply.py"

#: The literal Phase-1 verb set `pre_execution_directives()` (C1) ever
#: emits — the ONLY keys `_CLI_DISPATCH` may carry.
_EXPECTED_CLI_KEYS = {
    "pickup-assemble",
    "review-exec-auth-stamp",
    "session-claim-cli",
    "emit-dispatch-workflow",
}

#: The two other-ceremony verbs a "completing" reader of the B7 census might
#: mistakenly add — must never appear.
_FOREIGN_CEREMONY_VERBS = {"mint-deliverable-id", "advance-tracker-status"}


def _dotted_name(node: ast.AST) -> str:
    """Best-effort dotted-name reconstruction for an `ast.Attribute`/
    `ast.Name` chain, e.g. `coordinator_core.ops.dispatch_emit.emit.emit_script`."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def test_apply_never_imports_native_dispatch_emit():
    """No `import`/`from ... import` statement in `apply.py` names
    `coordinator_core.ops.dispatch_emit` or `emit_script` — the emit leg
    goes through the DoE script, never the native op."""
    tree = ast.parse(_APPLY_PATH.read_text(encoding="utf-8"), filename=str(_APPLY_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "dispatch_emit" not in alias.name, (
                    f"apply.py imports {alias.name!r} — the emit leg must go through "
                    "the DoE script, never coordinator_core.ops.dispatch_emit"
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert "dispatch_emit" not in module, (
                f"apply.py imports from {module!r} — the emit leg must go through "
                "the DoE script, never coordinator_core.ops.dispatch_emit"
            )
            for alias in node.names:
                assert alias.name != "emit_script", (
                    "apply.py imports emit_script — the emit leg must go through "
                    "the DoE script, never the native dispatch.emit op"
                )


def test_apply_never_calls_emit_script_or_dispatch_emit():
    """No call expression in `apply.py` resolves (by dotted name) to
    `emit_script` or `dispatch.emit` — a static scan, not an execution
    check, mirroring `test_no_bare_argv0_script_launch.py`'s own scope
    discipline."""
    tree = ast.parse(_APPLY_PATH.read_text(encoding="utf-8"), filename=str(_APPLY_PATH))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted_name(node.func)
        assert not dotted.endswith("emit_script"), (
            f"apply.py calls {dotted!r} — the emit leg must go through the DoE "
            "script, never the native op"
        )
        assert dotted != "dispatch.emit", (
            "apply.py calls dispatch.emit — the emit leg must go through the DoE "
            "script, never the native op"
        )


def test_cli_dispatch_key_set_is_exactly_the_four_phase1_verbs():
    """`_CLI_DISPATCH`'s key set is EXACTLY the four Phase-1 verbs
    `pre_execution_directives()` ever emits — no more, no fewer."""
    assert set(_CLI_DISPATCH.keys()) == _EXPECTED_CLI_KEYS


def test_cli_dispatch_excludes_foreign_ceremony_verbs():
    """`_CLI_DISPATCH` contains neither `mint-deliverable-id` nor
    `advance-tracker-status` — both belong to other ceremonies
    (`roadmap-planning`, `enrich-and-review`), not `/execute-plan`'s own
    SKILL.md."""
    keys = set(_CLI_DISPATCH.keys())
    overlap = keys & _FOREIGN_CEREMONY_VERBS
    assert not overlap, (
        f"_CLI_DISPATCH carries foreign-ceremony verb(s) {overlap!r} — these fire "
        "another ceremony's step out of its own ceremony"
    )
