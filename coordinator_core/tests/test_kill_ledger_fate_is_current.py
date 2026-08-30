"""The kill ledger's `Fate` line is checked against the live registry, not remembered.

Why this test exists, stated so nobody deletes it as bookkeeping:

`state/kill-ledger.md` is the roster a reader — and, more expensively, a
baton-minter — consults to answer *is this dead?*. Its `Status:` line records the
CUT EVENT and is deliberately never edited, so it goes stale by construction: an
op cut, rebuilt, and cut again carries a `Status:` describing only the first of
those three. K-057 (`handoff.reconcile_open`) is the worked example — its head
read CUT while its tail read "Returns-when, discharged" against a live 431ms
rebuild, and a spinoff was minted off that row asking for a THIRD build of a job
that already had a live home. The stale row cost a session.

The `Fate:` line answers the one question `Status:` cannot: **is this op key in
the registry today.** This test is what makes that answer true rather than
asserted — it goes red the moment a row marked DEAD has its op re-registered, or
a row marked LIVE has its op cut. That is the artifact discharging the rule; the
alternative ("someone re-reads the ledger") is exactly the thing that failed.

Vocabulary, closed:
  LIVE       — every op key in the heading is registered right now.
  DEAD       — no op key in the heading is registered; nothing owed.
  OPEN       — a nomination still undecided. This row owes work, live or not.
  NOT-AN-OP  — the cut removed code, not a registered op key.
  MIXED      — a multi-key entry only partly registered; the body adjudicates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.op_census.kill_ledger_inventory import fate_entries

LEDGER = Path(__file__).resolve().parents[2] / "state" / "kill-ledger.md"

# The published mirror ships `coordinator_core/` without claude-klabauter's `state/`
# corpus — working data is deliberately excluded from every publish set — so
# the ledger is absent there. The `parametrize` below reads it at COLLECTION
# time, which turns that absence into a collection ERROR for the whole tree
# rather than one failing test, and a collection error is what makes the
# end-of-run assembled-mirror gate refuse: every publish then closes FATAL with
# "treat this run's published bytes as unverified" (AC15 fail-closed), no
# matter what it shipped. Skipping at module level costs nothing where the
# corpus exists, which is the only place this guard's question has a subject.
if not LEDGER.is_file():  # pragma: no cover - only reachable in a published mirror
    pytest.skip(
        "state/kill-ledger.md is absent — no corpus here for this guard to check",
        allow_module_level=True,
    )

_VALUES = {"LIVE", "DEAD", "OPEN", "NOT-AN-OP", "MIXED"}


def _registry() -> dict:
    import coordinator_core.ops as ops

    try:
        ops._eager_import_all()
    except Exception:  # pragma: no cover - a partial import still registers most ops
        pass
    from coordinator_core.ipc import _REGISTRY

    return _REGISTRY


def _entries():
    """Yield (kid, heading, keys, fate_value) for every `## K-` entry.

    Sourced from `kill_ledger_inventory.fate_entries()` — the shared accessor
    both parsers now consume — rather than re-deriving heading text and Fate
    lines here. `LedgerAbsent` is not caught: the module-level `pytest.skip`
    above already handles the published-mirror case at collection time, so a
    `LedgerAbsent` reaching this point would be this test's own path
    disagreeing with the module-level check, which should fail loudly rather
    than be swallowed.
    """
    for entry in fate_entries(LEDGER):
        yield entry.key, entry.title, entry.op_keys, entry.fate_values


def test_every_entry_carries_exactly_one_fate_line():
    missing = [kid for kid, _h, _k, f in _entries() if len(f) != 1]
    assert not missing, (
        "kill-ledger entries without exactly one `**Fate (YYYY-MM-DD):** <VALUE>` "
        f"line (VALUE in {sorted(_VALUES)}): {missing}"
    )


@pytest.mark.parametrize("kid,heading,keys,fates", list(_entries()), ids=lambda v: str(v)[:24])
def test_fate_matches_the_live_registry(kid, heading, keys, fates):
    """LIVE iff every named op key is registered; DEAD iff none is.

    OPEN and NOT-AN-OP are deliberately exempt from the registry check: OPEN says
    the ROW owes a decision regardless of whether the op currently runs, and
    NOT-AN-OP says the cut had no op key to check. Both are still constrained --
    NOT-AN-OP must name no key, so it cannot be used to opt an op out.
    """
    fate = fates[0]
    reg = _registry()
    live = [k for k in keys if k in reg]

    if fate == "NOT-AN-OP":
        assert not keys, (
            f"{kid} is marked NOT-AN-OP but its heading names op key(s) {keys}. "
            "NOT-AN-OP is for cuts that removed code, not a registered op key -- "
            "it is not an opt-out for an op whose fate is inconvenient."
        )
        return

    if fate == "OPEN":
        return

    assert keys, (
        f"{kid} is marked {fate}, which is a claim about the registry, but its "
        "heading names no op key. Use NOT-AN-OP."
    )

    if fate == "LIVE":
        absent = [k for k in keys if k not in reg]
        assert not absent, (
            f"{kid} is marked LIVE but {absent} is/are absent from the registry. "
            "The op was cut or its registration stopped being imported -- update "
            "the Fate line (and say so in the entry), do not delete this test."
        )
    elif fate == "DEAD":
        assert not live, (
            f"{kid} is marked DEAD but {live} is/are registered right now. This is "
            "the K-057 failure recurring: a row read as dead while the op runs. "
            "Either the op came back (say so in the entry and mark it LIVE) or it "
            "was re-registered by accident."
        )
    elif fate == "MIXED":
        assert live and len(live) < len(keys), (
            f"{kid} is marked MIXED but its keys are uniformly "
            f"{'registered' if live else 'absent'}; use LIVE or DEAD."
        )
