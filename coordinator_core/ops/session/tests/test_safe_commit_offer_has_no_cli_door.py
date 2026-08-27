"""
coordinator_core.ops.session.tests.test_safe_commit_offer_has_no_cli_door —
pins `session.safe_commit_offer` to the warm door and keeps the interpreter
door deleted.

Purpose: on 2026-08-27 this module's `main()` CLI was replaced by the
`session.safe_commit_offer` op. The CLI was not merely slower — the
interpreter start WAS the defect it is deleted for, so a shim re-added over
the op would reinstate the whole cost while looking like a convenience:

    cmd.exe -> python (bin forwarder) -> exec -> python (trampoline -> module)

Measured on the normal tier, best of N, each figure including process start:
a bare interpreter is 43ms and importing this module is 146ms before the call
resolves a session id or touches git; the chain above paid that twice. The
whole round trip through `coordinator-invoke.exe` is 13ms. DR-344 § "Warm
engine, <50ms to reach it" makes an interpreter start ahead of warmth
break-class, and this op's sizing-object
(`dlv-the-safe-commit-offer-answers-on-the-exe-0899da`) records the rest.

Negative-spec — what this module deliberately does NOT do:
  - Does NOT measure process time or wall clock. The warm door's own timing
    gate (`benchmarks/tests/test_warm_door_process_time_gate.py`) already
    measures the door this op now answers on, and cloning that harness for
    one op would duplicate an isolated-warm-server fixture to re-measure a
    door already under budget. What is unguarded WITHOUT this module is not
    the timing — it is the door's SHAPE, which no timing test can see.
  - Does NOT assert the op commits anything. `test_safe_commit_offer.py`'s
    `TestHandler` owns behaviour; this module owns reachability.
"""

from __future__ import annotations

import ast
from pathlib import Path

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.op_scopes import _OP_KEY_SCOPE
from coordinator_core.ops import _EAGER_OP_MODULES, _eager_import_all

OP_KEY = "session.safe_commit_offer"
MODULE = "coordinator_core.ops.session.safe_commit_offer"
_SOURCE = Path(__file__).resolve().parent.parent / "safe_commit_offer.py"


def test_op_is_registered():
    _eager_import_all()
    assert OP_KEY in ipc._REGISTRY, (
        f"{OP_KEY} is not in the live registry — the ceremonies reach this "
        "module through the op and have no other door left"
    )


def test_op_is_eagerly_imported():
    # The scope-table parity gate reads a registry filled by _eager_import_all;
    # a lazily-mapped-only op has a scope row naming an op that gate cannot see.
    assert any(mod == MODULE for mod, _note in _EAGER_OP_MODULES), (
        f"{MODULE} is absent from _EAGER_OP_MODULES"
    )


def test_op_is_scoped_none():
    # "none", not common_dir: identity comes from the caller's `cwd` wire param,
    # never from the engine-supplied repo_root and never from this server
    # process's own environment — see the handler's docstring for why an
    # env-only read here commits under the wrong session's claim.
    assert _OP_KEY_SCOPE[OP_KEY] == "none"


def test_op_is_classified_mutating():
    # It commits and pushes. Ambiguous cases classify MUTATING; this one is not
    # even ambiguous.
    assert OP_CLASSIFICATION[OP_KEY] is OpClass.MUTATING


def test_module_defines_no_cli_entrypoint():
    """No `main`, no argv parsing, no `__main__` block — parsed, not grepped,
    so a docstring that merely MENTIONS the retired CLI (several do, as the
    historical record of this change) can never fail the test, and a real
    entrypoint can never pass it by being spelled differently.
    """
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))

    top_level_defs = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "main" not in top_level_defs, (
        "safe_commit_offer.main() is back. The interpreter start it needs is "
        "the defect this op was built to delete — see this module's docstring."
    )

    for node in tree.body:
        if isinstance(node, ast.If):
            src = ast.dump(node.test)
            assert "__main__" not in src, (
                "a `if __name__ == '__main__'` block is back in "
                "safe_commit_offer.py — it is reachable only by starting an "
                "interpreter, which is what this op exists to avoid"
            )


def test_module_does_not_import_sys_or_argparse():
    """The two imports a re-added CLI would need. `sys` and `json` both went
    unused the moment `main()` left, and an import of either reappearing is the
    earliest visible sign of a door being rebuilt.
    """
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "argparse" not in imported, "argparse is only ever needed by a CLI"
    assert "sys" not in imported, (
        "`sys` is back in safe_commit_offer.py — it was needed only by the "
        "deleted CLI's stderr writes and sys.exit()"
    )
