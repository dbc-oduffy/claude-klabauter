"""coordinator_core.hooks.fanin_registries — the fan-in layer, enumerated
once.

Arrival note (W4-C7, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/_fanin_registries.py`, with the
one shape change the move itself forces, plus one shape change the move
exposes as no-longer-applicable (see "WHAT CHANGED" below).

WHAT CHANGED — module loading. DoE's source loaded each fan-in
dispatcher/runner module off disk by FILENAME
(`importlib.util.spec_from_file_location`), because a DoE hooks-plane
script is not on any Python import path and its filename is not a legal
module name. That constraint does not exist here: every carrier this
module can enumerate is an ordinary package member of
`coordinator_core.hooks`, importable by its normal dotted name. This
module therefore imports carriers via `importlib.import_module`, never
`spec_from_file_location`, and the `FANIN_DISPATCHERS` keys below are
plain module names (`preuse_write_dispatch`), never `.py`-suffixed
filenames.

WHAT CHANGED — the registry shape a carrier exposes. DoE's fan-in
dispatchers each held a `REGISTRY`-shaped tuple (or, for two of them, a
sibling `_guard_runner.py`/`_stop_family_runner.py` module's own
`REAL_*_REGISTRY`) listing the doctrine-plane-LOCAL guards folded behind
that one hooks.json entry. Ported into this repo, that shape mostly
stopped existing: `preuse_write_dispatch.py`'s own arrival-note docstring
records that it carries ZERO local guards of its own any more — every
guard `evaluate()` discovers is a same-repo `write_guards/engine.py`
module, already enumerable via that module's own public
`discover_guard_names()`, so a second registry here would just duplicate
it. `stop_dispatch.py` similarly composes its eight legs as ordinary
Python calls, not a data-carrying registry object. Rather than fabricate a
registry these carriers no longer have, this module defines the
CONVENTION a carrier opts into if it genuinely fans out a doctrine-
plane-LOCAL guard set a manifest generator cannot otherwise see: a public
module-level `CARRIED_GUARDS: tuple[tuple[str, str], ...]` constant
(`(guard_id, script_tail)` pairs, `script_tail` a two-segment
`hooks/<module>.py`-shaped string). `carried_guards()` reads exactly that
constant and no other shape — a carrier landing later (its own out-of-
footprint chunk) that never grows a local guard set (like
`preuse_write_dispatch.py` today) correctly reports `not an enrolled
fan-in carrier` rather than a fabricated empty list, matching the source's
own "AttributeError, never a default" contract for a carrier it cannot
resolve.

Seven carriers named `FANIN_DISPATCHERS` DOWN-SELECTS to three, all that
exist in this repo as of this row's own landing:
`preuse_write_dispatch`, `stop_dispatch`, `postuse_advisory_dispatch`.
DoE's other four (`sessionstart-dispatch.py`, `sessionstart-async-
dispatch.py`, `preuse-bash-dispatch.py`, `postuse-stop-family-dispatch.py`)
have not landed in this repo yet — each is a NAMED chunk of its own in
this same plan (W4-C8/W4-C9 and siblings). Adding them here ahead of that
landing would import a module that does not exist; the correct move is to
extend `FANIN_DISPATCHERS` when that chunk lands its carrier, not to
pre-declare a key this module cannot resolve.

Negative-spec (unchanged from the source): this module does NOT execute
any guard, does NOT read `hooks.json` (which dispatcher is REGISTERED is
that file's question, and its readers' -- this one answers only what each
dispatcher CARRIES), and imports nothing outside the stdlib and this
package's own siblings.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C7
"""

from __future__ import annotations

import importlib
from typing import Callable, Dict, Tuple

FANIN_DISPATCHERS: Dict[str, str] = {
    "preuse_write_dispatch": "PreToolUse",
    "stop_dispatch": "Stop",
    "postuse_advisory_dispatch": "PostToolUse",
}


def load_carrier(module_name: str):
    return importlib.import_module(f"coordinator_core.hooks.{module_name}")


def carried_guards(module_name: str) -> "list[tuple[str, str]]":
    """`[(guard_id, script_tail), ...]` for one carrier, in declared
    order.

    `module_name` names the CARRIER (a `FANIN_DISPATCHERS` key), and must
    be a member of that map -- a name outside it raises `AttributeError`
    with the same "not an enrolled fan-in carrier" message the source used
    for the analogous case, so a caller's error handling does not need to
    special-case this port.

    Reads the carrier module's own public `CARRIED_GUARDS` tuple (see
    module docstring's "WHAT CHANGED" section) -- never a private-named
    alias, never a re-derivation by scanning the carrier's source. A
    carrier that has landed but declares no `CARRIED_GUARDS` (every
    carrier in `FANIN_DISPATCHERS` today) raises `AttributeError` naming
    exactly that -- correct, not a gap: those carriers fold zero
    doctrine-plane-local guards today, and a manifest generator asking
    "what does this carrier deliver beyond what it already gets from an
    engine-side roster export" should get a loud "nothing declared" rather
    than a silently fabricated empty success.
    """
    if module_name not in FANIN_DISPATCHERS:
        raise AttributeError(f"{module_name} is not an enrolled fan-in carrier")
    module = load_carrier(module_name)
    rows = getattr(module, "CARRIED_GUARDS", None)
    if rows is None:
        raise AttributeError(
            f"{module_name} exposes no CARRIED_GUARDS -- not a doctrine-plane-local "
            "fan-in carrier (it may still deliver guards read from an engine-side "
            "roster export instead; see this module's own docstring)"
        )
    return [(guard_id, script_tail) for guard_id, script_tail in rows]


def all_carried_guards() -> Dict[str, str]:
    """`{guard_id: carrier_module_name}` across every fan-in carrier that
    declares a `CARRIED_GUARDS` roster.

    Fails loud on a guard carried by two carriers: that is a double-
    delivery, not a naming collision, and must never be resolved silently
    by last-writer-wins. A carrier declaring no `CARRIED_GUARDS` at all is
    skipped rather than raised on here -- `all_carried_guards()` answers
    "what IS declared", and an undeclared carrier contributes nothing to
    that union; call `carried_guards()` directly for the loud per-carrier
    error.
    """
    seen: Dict[str, str] = {}
    for carrier in FANIN_DISPATCHERS:
        try:
            rows = carried_guards(carrier)
        except AttributeError:
            continue  # carrier declares no CARRIED_GUARDS; contributes nothing to the union
        for guard_id, _script_tail in rows:
            if guard_id in seen:
                raise ValueError(
                    f"{guard_id} is carried by both {seen[guard_id]} and "
                    f"{carrier} -- double delivery, not a naming collision"
                )
            seen[guard_id] = carrier
    return seen
