"""Pins `scripts/setup.py :: main`'s step ORDER (chunk C3, second file):
`offer_warm_opt_in` before `install_bin_forwarders` before `install_warm_door`
-- sibling in intent to `test_door_bare_name_ordering.py`, which pins the
adjacent `install_bin_forwarders` / `install_warm_door` pair.

The ordering is what makes a pre-install listener the normal case: a warm
listener started (or opted into) before the door and its forwarders are
(re)installed is the state F-022's fixture (C3's own hot-path test) actually
exercises -- a warm listener resident FIRST, door images installed SECOND.
A silent reorder here -- `install_bin_forwarders`/`install_warm_door` moved
ahead of `offer_warm_opt_in` -- would retire that fixture's premise without
anyone noticing: the listener would start AFTER the door/forwarders exist,
which is a different install-time shape than the one the regression fixture
was written against.

Negative-spec:
    - Does not test that `offer_warm_opt_in` actually brings a listener up,
      or that `install_bin_forwarders` / `install_warm_door` actually
      install anything -- those are each function's own behavioural
      coverage. This module tests only the three calls' relative order in
      `scripts/setup.py :: main`, which is a property none of those
      per-function tests can see.
    - Does not duplicate `test_door_bare_name_ordering.py`'s
      `install_bin_forwarders` < `install_warm_door` pin, or its
      `claim_bare_name` coupling check -- that pair, and the reason it
      matters (PowerShell's `.ps1`-over-`.exe` ranking), is that module's
      own. This module adds the one edge neither of that module's tests
      cover: where `offer_warm_opt_in` falls relative to both.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SETUP_PY = Path(__file__).resolve().parents[3] / "scripts" / "setup.py"

#: The three calls whose relative order is the whole guarantee.
_WARM_OPT_IN = "offer_warm_opt_in"
_FORWARDERS = "install_bin_forwarders"
_DOOR = "install_warm_door"


def _main_body_call_order() -> list[str]:
    """Names of the functions called directly in `setup.py :: main`, in
    source order. Parsed rather than imported -- same reason as
    `test_door_bare_name_ordering.py`'s own helper: `scripts/setup.py` is a
    standalone installer that must run before this package is importable,
    so importing it here would invert the very dependency it exists to
    bootstrap."""
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


def test_warm_opt_in_precedes_forwarder_installation() -> None:
    order = _main_body_call_order()

    assert _WARM_OPT_IN in order, (
        f"{_WARM_OPT_IN} is no longer called from setup.py :: main. If the "
        f"warm opt-in step moved, this ordering guarantee moved with it -- "
        f"re-anchor this test rather than deleting it."
    )
    assert _FORWARDERS in order, (
        f"{_FORWARDERS} is no longer called from setup.py :: main. If "
        f"forwarder emission moved, re-anchor this test."
    )

    warm_at = order.index(_WARM_OPT_IN)
    forwarders_at = order.index(_FORWARDERS)

    assert warm_at < forwarders_at, (
        f"setup.py :: main calls {_FORWARDERS} (index {forwarders_at}) BEFORE "
        f"{_WARM_OPT_IN} (index {warm_at}). That order installs forwarders "
        f"against a box that has not yet been offered a warm listener, which "
        f"is a different install-time shape than the one the F-022 regression "
        f"fixture (test_hot_path_records_warm_route.py's post-install "
        f"ordering case) exercises -- a warm listener resident FIRST, door "
        f"images installed SECOND. Restore the order."
    )


def test_warm_opt_in_precedes_door_installation() -> None:
    order = _main_body_call_order()

    assert _WARM_OPT_IN in order, (
        f"{_WARM_OPT_IN} is no longer called from setup.py :: main -- re-anchor "
        f"this test rather than deleting it."
    )
    assert _DOOR in order, (
        f"{_DOOR} is no longer called from setup.py :: main -- re-anchor this test."
    )

    warm_at = order.index(_WARM_OPT_IN)
    door_at = order.index(_DOOR)

    assert warm_at < door_at, (
        f"setup.py :: main calls {_DOOR} (index {door_at}) BEFORE "
        f"{_WARM_OPT_IN} (index {warm_at}). Same hazard as the forwarder "
        f"pairing above: the door would be installed against a box not yet "
        f"offered a warm listener, retiring the F-022 regression fixture's "
        f"pre-install-listener premise without anyone noticing. Restore the "
        f"order."
    )


def test_the_full_three_step_order_is_warm_opt_in_then_forwarders_then_door() -> None:
    """The two pairwise checks above are necessary, not sufficient: this
    pins the full three-way ORDER directly, so a reorder that keeps each
    pairwise relation true only by accident (impossible with three items,
    but pinned explicitly rather than left to be inferred from two separate
    tests) fails loudly on its own."""
    order = _main_body_call_order()

    for name in (_WARM_OPT_IN, _FORWARDERS, _DOOR):
        assert name in order, (
            f"{name} is no longer called from setup.py :: main -- re-anchor this test."
        )

    positions = [order.index(_WARM_OPT_IN), order.index(_FORWARDERS), order.index(_DOOR)]

    assert positions == sorted(positions), (
        f"setup.py :: main's call order for {_WARM_OPT_IN!r}, {_FORWARDERS!r}, "
        f"{_DOOR!r} is {positions} (index positions), not strictly increasing "
        f"-- the required order is {_WARM_OPT_IN} then {_FORWARDERS} then "
        f"{_DOOR}. Restore it."
    )
