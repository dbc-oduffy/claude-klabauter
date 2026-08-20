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
    assert not dual_reads, (
        f"cc_invoke still has {len(dual_reads)} expression(s) reading BOTH pinned names "
        f"(first-consulted: {dual_reads}). C14 CLOSED the dual-read window, so the "
        "carve-out must consult the NEW name alone. Until C14 this assertion ran the "
        "other way round and required both to be present.\n\n"
        "This inversion caught a real miss: C14's first pass changed only the rung-1 "
        "read and the child-env write, leaving TWO further dual-read rungs in this file "
        "untouched. A precedence-order check cannot see that — both remaining sites put "
        "the new name first and would have passed — which is why the post-C14 assertion "
        "is PRESENCE, not order."
    )


def test_accessor_no_longer_falls_back_now_the_window_is_closed(monkeypatch, capsys):
    """Pins the window CLOSED. This test previously asserted the opposite, and its
    docstring called its own failure "the signal that the fallback was dropped early".

    It fired exactly as designed when C14 dropped the fallback. That is a tripwire doing
    its job, not a stale guard: the correct response is to invert it deliberately, here,
    rather than to delete it -- which is what would have happened if it had been written
    as a bare assertion with no explanation of what its failure MEANT.

    C14 dropped the fallback on purpose, on precondition items 1-3 rather than item 4's
    N-days-of-zero proxy (see the plan's C14 gate `cleared_evidence`). The old name is
    still READ, solely to name itself retired, so a consumer that has not converged gets
    a named cause instead of a silent None several rungs downstream.
    """
    from coordinator_core import engine_root

    monkeypatch.delenv(engine_root._ENGINE_ROOT_NEW_VAR, raising=False)
    monkeypatch.setenv(engine_root._ENGINE_ROOT_OLD_VAR, "old-name-answer")
    engine_root._reset_engine_root_env_advisories()

    assert engine_root.coordinator_engine_root_env("test.ac13_window") is None
    err = capsys.readouterr().err
    assert "NO LONGER HONOURED" in err
    assert engine_root._ENGINE_ROOT_NEW_VAR in err


def test_carveout_precedence_pins_the_new_name_only():
    """AC13's pin, POST-C14: cc_invoke's hand-duplicated rung must read the new name and
    must NOT fall back -- the same edit the accessor took. Requested by doe-claude-em as
    an engine-axis-suffix pin rather than a literal-equality one, because their own ladder
    leads with REPO_CLAUDE_KLABAUTER on the LOCATOR axis and pinning them equal would pin
    the wrong claim.
    """
    src = _CC_INVOKE.read_text(encoding="utf-8")

    assert 'existing = os.environ.get(_ENGINE_ROOT_NEW_VAR, "")\n' in src
    assert (
        'os.environ.get(_ENGINE_ROOT_NEW_VAR, "") or os.environ.get(_ENGINE_ROOT_OLD_VAR'
        not in src
    ), "cc_invoke still falls back to the retired name; C14 removed that rung"
    assert "_ENGINE_ROOT_OLD_VAR: claude_klabauter_root" not in src, (
        "cc_invoke still exports the retired name into child environments"
    )


def test_accessor_returns_none_when_neither_name_is_set(monkeypatch):
    """The accessor must not invent a value the caller did not have."""
    from coordinator_core import engine_root

    monkeypatch.delenv(engine_root._ENGINE_ROOT_NEW_VAR, raising=False)
    monkeypatch.delenv(engine_root._ENGINE_ROOT_OLD_VAR, raising=False)

    assert engine_root.coordinator_engine_root_env("test.ac13_absent") is None
