"""Ratchet: every halt kind is mechanical, irreversible, a PM-ruled keeper, or frozen recoverable debt."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit import halt_remedy_class as hrc
from coordinator_core.ops.dispatch_emit.tests.test_no_mechanical_halt_reaches_em import _halt_constants
from coordinator_core.ops.dispatch_emit.ask_contract import REGISTER_ROWS_UNCLAIMED_REFUSAL
from coordinator_core.ops.dispatch_emit.landing_targeted_tests import (
    REVERIFY_INDETERMINATE,
    REVIEW_REVERIFY_FAILED,
    REVIEW_REVERIFY_INDETERMINATE,
    TARGETED_TESTS_FAILED,
    ZERO_FAIL_UNATTRIBUTED,
)

#: The debt only shrinks: lowering this is the only edit a conversion makes here.
RECOVERABLE_DEBT_CEILING = 0
PM_RULED_KEEPER_KINDS = {"stop_after-refusal"}
_CHAIN_PREFIXES = ("ask_", "sizing_fire", "land", "halt")


def _emitted_kinds() -> set[str]:
    # self_correction's HALT_* are stage labels and phases, not halt kinds.
    kinds = {v for k, v in _halt_constants().items() if not k.startswith("self_correction.py:")}
    kinds.update({
        REGISTER_ROWS_UNCLAIMED_REFUSAL, TARGETED_TESTS_FAILED, REVIEW_REVERIFY_FAILED,
        REVERIFY_INDETERMINATE, REVIEW_REVERIFY_INDETERMINATE, ZERO_FAIL_UNATTRIBUTED,
    })
    return kinds


def _bucket(kind: str) -> list[str]:
    return [
        name for name, hit in (
            ("mechanical", hrc.is_mechanical(kind)),
            ("irreversible", hrc.is_irreversible(kind)),
            ("pm-ruled-keeper", kind in hrc.PM_RULED_KEEPER),
            ("recoverable-debt", kind in hrc.RECOVERABLE_DEBT),
        ) if hit
    ]


def test_every_halt_kind_is_in_exactly_one_bucket():
    kinds = _emitted_kinds() | set(hrc.HALT_REMEDY)
    bad = {k: _bucket(k) for k in sorted(kinds) if len(_bucket(k)) != 1}
    assert not bad, (
        "a halt kind must be MECHANICAL (remedy_or_halt), IRREVERSIBLE_KINDS (harm cited), PM_RULED_KEEPER or "
        f"RECOVERABLE_DEBT, exactly one: {bad}"
    )


def test_irreversible_and_keeper_entries_cite_their_reason():
    assert not [k for k, why in hrc.IRREVERSIBLE_KINDS.items() if not why.strip()]
    assert set(hrc.PM_RULED_KEEPER) == PM_RULED_KEEPER_KINDS
    assert all("PM" in cite and ".md" in cite for cite in hrc.PM_RULED_KEEPER.values())
    assert hrc.IRREVERSIBLE == "irreversible"


def test_recoverable_debt_only_shrinks():
    assert len(hrc.RECOVERABLE_DEBT) <= RECOVERABLE_DEBT_CEILING, (
        "RECOVERABLE_DEBT grew: a new recoverable halt kind must be MECHANICAL with a remedy, not new debt"
    )
    assert all(k in hrc.HALT_REMEDY for k in hrc.RECOVERABLE_DEBT), "debt names a kind the registry lacks"
    assert all(hrc.HALT_REMEDY[k][0] == hrc.DECISION for k in hrc.RECOVERABLE_DEBT)
    assert all(m.endswith(".py") for m in hrc.RECOVERABLE_DEBT.values())


def test_a_new_unclassified_kind_is_refused(monkeypatch):
    monkeypatch.setitem(hrc.HALT_REMEDY, "brand-new-halt", (hrc.DECISION, None))
    assert _bucket("brand-new-halt") == []


def test_recoverable_debt_holds_no_ask_warp_landing_chain_kind():
    chain = {k: m for k, m in hrc.RECOVERABLE_DEBT.items() if m.startswith(_CHAIN_PREFIXES)}
    assert not chain, f"recoverable halt kinds still unconverted in the ask/warp/landing chain: {chain}"
