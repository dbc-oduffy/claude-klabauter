"""The door only wins the bare name because a removal runs after a write.

`install_bin_forwarders` emits `coordinator-invoke.ps1` on every install
(`substrate._emit_and_verify_ps1_forwarders`), and PowerShell ranks a
same-directory `.ps1` ABOVE the door's `.exe` -- so that sibling, if it
survives, silently takes every PowerShell caller off the ~2.34ms native door
and back onto a cold interpreter start (~39ms interpreter + ~55ms engine
import, measured 2026-08-26). `install_warm_door` ends by calling
`door_install.claim_bare_name`, which strips it.

Nothing in the type system, the call signatures, or the two functions'
docstrings couples those steps: they are adjacent lines in
`scripts/setup.py :: main`, and the whole guarantee is that one runs after the
other. There is no error on the wrong order and no runtime signal -- the door
is simply never reached, and the only symptom is a number nobody is watching.

`door_install.py`'s own docstring already records that the `.ps1` "is now a
certainty on every install", so this is not a hypothetical kept true by
absence; it is a live write, defused by ordering alone. This module pins the
ordering so the defusal cannot be reordered away in silence.

Negative-spec: this does NOT test that the removal works -- `test_door_install.py`
owns `_remove_shadowing_forwarder_siblings` / `claim_bare_name` behaviour. This
tests only that the removal is SEQUENCED after the write, which is the property
no behavioural test of either function alone can see.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SETUP_PY = Path(__file__).resolve().parents[3] / "scripts" / "setup.py"

#: The two calls whose relative order is the whole guarantee, and the removal
#: entry point that makes the second one load-bearing.
_WRITER = "install_bin_forwarders"
_REMOVER = "install_warm_door"
_REMOVAL_ENTRY = "claim_bare_name"


def _main_body_call_order() -> list[str]:
    """Names of the functions called directly in `setup.py :: main`, in source
    order. Parsed rather than imported: `scripts/setup.py` is a standalone
    installer that must run before this package is importable, so importing it
    here would invert the very dependency it exists to bootstrap."""
    tree = ast.parse(_SETUP_PY.read_text(encoding="utf-8"))
    main = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"),
        None,
    )
    assert main is not None, "scripts/setup.py has no top-level main() -- this test's anchor moved"

    calls: list[str] = []
    for node in ast.walk(main):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.append(node.func.id)
    return calls


def test_forwarder_emission_precedes_the_door_bare_name_claim() -> None:
    order = _main_body_call_order()

    assert _WRITER in order, (
        f"{_WRITER} is no longer called from setup.py :: main. If forwarder "
        f"emission moved, this ordering guarantee moved with it -- re-anchor "
        f"this test rather than deleting it."
    )
    assert _REMOVER in order, (
        f"{_REMOVER} is no longer called from setup.py :: main. The door's "
        f"bare-name claim is what keeps PowerShell on the native path; if the "
        f"door install moved, find where {_REMOVAL_ENTRY} now runs and re-anchor."
    )

    writer_at = order.index(_WRITER)
    remover_at = order.index(_REMOVER)

    assert writer_at < remover_at, (
        f"setup.py :: main calls {_REMOVER} (index {remover_at}) BEFORE "
        f"{_WRITER} (index {writer_at}). That order re-emits "
        f"coordinator-invoke.ps1 after the door stripped it, and PowerShell "
        f"ranks .ps1 above .exe -- so every PowerShell caller silently reverts "
        f"from the native door to a cold interpreter start. There is no error "
        f"on this path; the only symptom is process time. Restore the order."
    )


def test_no_forwarder_emission_after_the_door_claims_the_bare_name() -> None:
    """The pairwise order above is necessary, not sufficient: a LATER step that
    re-emits forwarders would re-create the shadowing sibling after the removal
    has already run. Nothing does today; this fails loudly if one is added."""
    order = _main_body_call_order()
    remover_at = order.index(_REMOVER)

    late_writers = [
        (i, name) for i, name in enumerate(order[remover_at + 1:], start=remover_at + 1)
        if name == _WRITER
    ]

    assert not late_writers, (
        f"setup.py :: main calls {_WRITER} again at index(es) "
        f"{[i for i, _ in late_writers]}, after {_REMOVER} has already claimed "
        f"the bare name. That re-writes coordinator-invoke.ps1 and re-shadows "
        f"the door for PowerShell callers. Either move the emission before the "
        f"door install, or call door_install.{_REMOVAL_ENTRY} again after it."
    )


def test_the_removal_entry_point_is_still_what_the_door_install_calls() -> None:
    """If `install_warm_door` stops calling `claim_bare_name`, the ordering
    pinned above still passes while protecting nothing -- the exact
    green-but-vacuous shape this repo has been bitten by. Anchor the coupling."""
    source = _SETUP_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    door_fn = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == _REMOVER),
        None,
    )
    assert door_fn is not None, f"scripts/setup.py :: {_REMOVER} not found -- re-anchor this test"

    attr_calls = {
        node.func.attr
        for node in ast.walk(door_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert _REMOVAL_ENTRY in attr_calls, (
        f"{_REMOVER} no longer calls door_install.{_REMOVAL_ENTRY}. The ordering "
        f"test above would still pass, but nothing strips coordinator-invoke.ps1 "
        f"and PowerShell callers are back on the cold path. Restore the call, or "
        f"re-anchor both tests onto whatever replaced it."
    )
