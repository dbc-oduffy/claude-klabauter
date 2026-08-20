"""AC13's second clause: the bootstrap carve-out is PINNED EQUAL to the accessor.

Spec backlink: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md § AC13, C11.

AC13 allows exactly one canonical reader of the engine-root env var, PLUS a named,
enumerated bootstrap carve-out. `coordinator/bin/lib/cc_invoke.py` is on that list because
resolving `coordinator_core` is the very thing it exists to bootstrap -- it cannot import
the accessor without a circularity -- so it duplicates the precedence rule by hand.

A duplicated rule that nothing pins is a rule with two homes and one of them silently rots.
That is not hypothetical here: this plan has already had one guard go stale against a rename
it was meant to protect (AC12's fixture, fixed at d78c164fe055), failing INERT rather than
loudly. These tests exist so the carve-out cannot drift from the accessor the same way.

WHY BY AST AND NOT BY IMPORT: `cc_invoke.py` lives under `coordinator/bin/lib/`, is not an
importable package member, and bootstraps sys.path as a side effect of being imported.
Reading its constants out of the source text is what lets this test assert on the deployed
file without executing its bootstrap.

NEGATIVE SPEC -- what these tests deliberately do NOT assert:
  - Not that the two files' TEXT matches. The duplication is intentional; only the VALUES
    and the PRECEDENCE are contract.
  - Not anything about the published mirror's spelling. The publish transform rewrites the
    old name in both files together, so cross-tree parity is AC17's, not this file's. These
    assertions are within-tree by construction and would be wrong to widen.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CC_INVOKE = _REPO_ROOT / "coordinator" / "bin" / "lib" / "cc_invoke.py"
_PINNED_NAMES = ("_ENGINE_ROOT_NEW_VAR", "_ENGINE_ROOT_OLD_VAR")


def _module_level_str_constants(path: pathlib.Path) -> dict[str, str]:
    """Module-level `NAME = "literal"` assignments, read without importing."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            found[target.id] = node.value.value
    return found


def _dual_read_expressions(path: pathlib.Path) -> list[str]:
    """For every boolean expression that reads BOTH pinned names, which is consulted first.

    Returns one entry per such expression, naming the constant that appears first in it.
    Per-expression rather than per-file on purpose: comparing the two names' offsets across
    the whole source cannot see a single inverted site, because the other sites still put
    the new name earlier. That weaker check passed against a deliberately inverted
    carve-out, which is why this reads the AST instead.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    ordered: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BoolOp):
            continue
        names = [
            sub.id
            for sub in ast.walk(node)
            if isinstance(sub, ast.Name) and sub.id in _PINNED_NAMES
        ]
        if set(names) == set(_PINNED_NAMES):
            ordered.append(names[0])
    return ordered


@pytest.fixture(scope="module")
def carve_out_constants() -> dict[str, str]:
    assert _CC_INVOKE.is_file(), f"AC13's named carve-out is missing from disk: {_CC_INVOKE}"
    return _module_level_str_constants(_CC_INVOKE)


def test_carve_out_declares_both_pinned_names(carve_out_constants):
    missing = [n for n in _PINNED_NAMES if n not in carve_out_constants]
    assert not missing, (
        f"cc_invoke.py no longer declares {missing}. AC13's carve-out must duplicate the "
        "accessor's precedence rule EXPLICITLY, via named constants that this test can pin. "
        "Inlining the literals back into the call sites removes the seam that makes the "
        "duplication auditable."
    )


@pytest.mark.parametrize("const_name", _PINNED_NAMES)
def test_carve_out_constant_equals_accessor_constant(const_name, carve_out_constants):
    """The hand-duplicated names must equal `engine_root`'s own."""
    from coordinator_core import engine_root

    accessor_value = getattr(engine_root, const_name)
    carve_out_value = carve_out_constants[const_name]

    assert carve_out_value == accessor_value, (
        f"{const_name} has drifted: cc_invoke.py says {carve_out_value!r}, "
        f"coordinator_core.engine_root says {accessor_value!r}. These are the SAME rule "
        "written twice because the carve-out cannot import the accessor; when they "
        "disagree, a bootstrapping caller and the engine resolve different variables and "
        "nothing fails loudly."
    )


def test_carve_out_precedence_matches_accessor(monkeypatch):
    """New name wins over old, in the carve-out exactly as in the accessor.

    Equal constant VALUES are not sufficient -- a carve-out that read the two names in the
    opposite order would satisfy the pin above while behaving the wrong way round, and the
    accessor's own docstring calls this precedence load-bearing: a stale CLAUDE_KLABAUTER_ROOT
    inherited from an ancestor process must never override a fresh COORDINATOR_ENGINE_ROOT
    set by the immediate parent.
    """
    from coordinator_core import engine_root

    monkeypatch.setenv(engine_root._ENGINE_ROOT_NEW_VAR, "new-name-answer")
    monkeypatch.setenv(engine_root._ENGINE_ROOT_OLD_VAR, "old-name-answer")

    assert engine_root.coordinator_engine_root_env("test.ac13_precedence") == "new-name-answer"

    dual_reads = _dual_read_expressions(_CC_INVOKE)
    assert dual_reads, (
        "cc_invoke no longer contains any expression reading BOTH pinned names. The "
        "dual-read window is open until C14, so both must still be consulted."
    )
    inverted = [first for first in dual_reads if first != "_ENGINE_ROOT_NEW_VAR"]
    assert not inverted, (
        f"{len(inverted)} of {len(dual_reads)} dual-read expressions in cc_invoke consult "
        "the OLD engine-root name before the NEW one. The accessor gives the new name "
        "precedence, so this inverts the contract for exactly the callers that cannot "
        "import the accessor to get it right. Checked per-expression via AST: an earlier "
        "version of this test compared string offsets of the two names across the whole "
        "file, which a single inverted site does not change -- it passed against a "
        "deliberately inverted carve-out."
    )


def test_accessor_falls_back_to_old_name_while_the_window_is_open(monkeypatch):
    """Pins the window itself: with only the old name set, the accessor still answers.

    C14 closes this window. Until it does, a reader must get the right answer from EITHER
    name (AC14) -- so this failing is the signal that the fallback was dropped early, which
    would strand every consumer that has not yet converged.
    """
    from coordinator_core import engine_root

    monkeypatch.delenv(engine_root._ENGINE_ROOT_NEW_VAR, raising=False)
    monkeypatch.setenv(engine_root._ENGINE_ROOT_OLD_VAR, "old-name-answer")

    assert engine_root.coordinator_engine_root_env("test.ac13_window") == "old-name-answer"


def test_accessor_returns_none_when_neither_name_is_set(monkeypatch):
    """The accessor must not invent a value the caller did not have."""
    from coordinator_core import engine_root

    monkeypatch.delenv(engine_root._ENGINE_ROOT_NEW_VAR, raising=False)
    monkeypatch.delenv(engine_root._ENGINE_ROOT_OLD_VAR, raising=False)

    assert engine_root.coordinator_engine_root_env("test.ac13_absent") is None
