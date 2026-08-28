"""One dispatchable housekeeping key, and one module allowed to reach the leg.

Governing plan:
`docs/plans/2026-08-27-one-corpus-read-or-the-housekeeping-job-dies-a-fourth-time.md`,
chunk C6 — the half of the prime exit criterion a timing test cannot see. The
timing half lives in
`coordinator_core/reconcile/tests/test_housekeeping_corpus_read_budget.py`.

WHY THIS IS NOT `test_op_suspension_ratchet.py`, whose ground is adjacent and
which was the chunk's own home-check candidate. That module polices the
ROSTER: that it never gains a row silently, that a removal prunes the ratified
floor, that every row carries its measured evidence, and that every row on the
LIVE roster is refused at both doors. Its assertions parametrise over
`SUSPENDED_OPS` and say nothing about any particular job. What is asserted here
is the shape of ONE job — which single key is dispatchable for handoff
housekeeping, and which single module may reach the archival leg as a library —
and neither fact is a roster fact. A reader editing the roster would not think
to look at it, and a reader editing this job would not think to look in the
roster guard. Same directory, own file, cross-referenced both ways.

Its behavioural leg `test_suspended_op_cannot_be_resolved_for_in_process_
invocation` already covers half of (a) below, generically. It is restated here
for the three named keys deliberately: that test asserts "whatever is on the
roster is refused", which stays green if a key silently LEAVES the roster. This
one names the three and fails if any of them becomes reachable again, by any
route.

REGISTRY MEMBERSHIP IS THE WRONG PREDICATE, and not merely the wrong phrasing —
it is non-deterministic. `ipc._REGISTRY` holds 0 ops after `import
coordinator_core.ops` alone and 2 after importing one op module directly, so a
membership assertion's verdict depends on what an unrelated test imported first.
It is also blind to shape: `handoff_reconcile` registers through the
FUNCTION-CALL form `register_op("handoff.reconcile_open", _handler)`, which a
decorator grep misses, while `handoff.archive_transition` was killed by removing
its decorator entirely. Three kills, two shapes, and a fourth shape is one split
away. DISPATCHABILITY is the property that actually matters and the only one
that is stable: what does `get_op_handler` do when a caller asks for the name.

Negative-spec:
  - Asserts nothing about timing, spawn count, or corpus size. That is the
    sibling module's job and DR-344 forbids resting a conclusion here on a
    figure this file did not take.
  - Does not assert registry MEMBERSHIP, decorator shape, or import order, for
    the reason above.
  - Does not re-test the op's own composition contract
    (`coordinator_core/ops/tests/test_handoff_housekeeping.py`) or d6's unwrap
    (`coordinator_core/baton_assemble/tests/test_d6_routes_through_housekeeping.py`).
  - Does not police the roster. `test_op_suspension_ratchet.py` owns that, and a
    change to which ops are suspended must fail there, not here.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Set

import pytest

from coordinator_core import ipc, op_budget_suspension

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The three keys the 200ms sweep killed for the cost of a corpus walk they
#: CALLED rather than work they did. Kill means kill forever (PM 2026-08-23);
#: none of them comes back, and the surviving computes are reached as libraries
#: by the one job that replaced them.
_LEGACY_KEYS = (
    "handoff.reconcile_open",
    "handoff.archive_transition",
    "session.sweep_consumed_handoffs",
)

#: The one live key. Named as a literal rather than read off
#: `handoff_housekeeping.OP_KEY`, so a rename of the op does not silently
#: rename what this guard asserts.
_LIVE_KEY = "handoff.housekeeping"

#: The archival leg, as a module path and the attribute that IS the leg.
_LEG_MODULE = "coordinator_core.ops.handoff_archive_transition"
_LEG_ATTR = "_handler"

#: The ONE job, and the only production module permitted to reach the leg
#: directly. `coordinator_core/archive_stamp.py` was the third door onto this
#: compute until 2026-08-28 and was redirected through `handoff.housekeeping`
#: in this plan's C4; if it reappears below, the redirect has been reverted.
_PERMITTED_DIRECT_IMPORTERS = frozenset({
    "coordinator_core/ops/handoff_housekeeping.py",
})

#: Production trees only. Test modules legitimately import the leg to exercise
#: it (`coordinator_core/ops/tests/test_supersede_archives_atomically.py`,
#: `coordinator/bin/tests/test_handoff_archive_transition.py`); a guard that
#: forbade that would be asserting the compute has no tests.
_SCAN_ROOTS = ("coordinator_core", "coordinator/bin", "bin", "scripts")


def _resolve_or_none(key: str):
    """`get_op_handler` has TWO refusal shapes and only one of them is a return
    value (its own docstring): a suspended or killed op RAISES
    `OpSuspendedError`, an unregistered one returns None. Both are the same
    answer to this file's question — the key is not dispatchable — so both fold
    into None here."""
    try:
        return ipc.get_op_handler(key)
    except op_budget_suspension.OpSuspendedError:
        return None


def _is_test_path(path: Path) -> bool:
    if "tests" in path.parts:
        return True
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py")


def _production_sources() -> List[Path]:
    found: List[Path] = []
    for root in _SCAN_ROOTS:
        base = _REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if _is_test_path(path):
                continue
            found.append(path)
    return found


def _dotted(node: ast.AST) -> str:
    """Render an Attribute/Name chain as a dotted string, or '' for anything
    else. `a.b.c` -> 'a.b.c'."""
    parts: List[str] = []
    cursor: ast.AST = node
    while isinstance(cursor, ast.Attribute):
        parts.append(cursor.attr)
        cursor = cursor.value
    if not isinstance(cursor, ast.Name):
        return ""
    parts.append(cursor.id)
    return ".".join(reversed(parts))


def _reaches_the_leg_directly(source: str) -> bool:
    """True if this module binds `handoff_archive_transition._handler` by any of
    the three shapes that exist in this tree.

    Shape 1: `from ...handoff_archive_transition import _handler [as X]`.
    Shape 2: `import ...handoff_archive_transition as hat` then `hat._handler`.
    Shape 3: the fully dotted `coordinator_core.ops.handoff_archive_transition.
             _handler`, which needs no alias at all.

    Asserted on the CALL GRAPH rather than on the op key, because the key is
    dead and a module reaching the leg does so without ever naming it.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    aliases: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == _LEG_MODULE:
                for alias in node.names:
                    if alias.name == _LEG_ATTR:
                        return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _LEG_MODULE and alias.asname:
                    aliases.add(alias.asname)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr != _LEG_ATTR:
            continue
        dotted = _dotted(node)
        if dotted == f"{_LEG_MODULE}.{_LEG_ATTR}":
            return True
        head = dotted.split(".", 1)[0] if dotted else ""
        if head and head in aliases:
            return True
    return False


# --- (a) the three killed keys are not dispatchable, by either door ---------


@pytest.mark.parametrize("key", _LEGACY_KEYS)
def test_a_legacy_key_does_not_resolve_to_a_handler(key: str) -> None:
    """Either refusal shape is correct; a callable is not.

    This is the assertion the plan's exit criterion actually rests on, and it is
    indifferent to HOW the key was killed — roster entry, decorator removal, or
    a fourth mechanism a later split invents. A caller that asks for the name
    gets nothing back, and that is the whole property.
    """
    assert _resolve_or_none(key) is None, (
        f"{key} resolved to a live handler. It is one of the three keys the "
        f"200ms sweep killed; kill means kill forever (PM 2026-08-23). The "
        f"surviving compute is reached as a library by {_LIVE_KEY}, never by "
        f"re-registering this name."
    )


def test_the_legacy_keys_are_refused_loudly_not_silently_absent() -> None:
    """A killed key must stay distinguishable from a name that never existed.

    `get_op_handler` returning None for a killed op degrades the refusal to
    METHOD_NOT_FOUND at the dispatch door, which reads to an operator as "you
    typed it wrong" rather than "this was measured and turned off". The three
    keys are gravestones, and a gravestone that stops refusing is just a hole.
    """
    for key in _LEGACY_KEYS:
        assert key in op_budget_suspension.SUSPENDED_OPS, (
            f"{key} left SUSPENDED_OPS, so asking for it now returns None "
            f"instead of raising OpSuspendedError — the refusal went quiet. "
            f"Roster bookkeeping is test_op_suspension_ratchet.py's; what "
            f"fails here is that the kill stopped being loud."
        )
        with pytest.raises(op_budget_suspension.OpSuspendedError):
            ipc.get_op_handler(key)


# --- (b) exactly one housekeeping key is dispatchable -----------------------


def test_exactly_one_housekeeping_key_resolves_to_a_live_handler() -> None:
    """ONE job, one door. The plan's own title is the assertion.

    Counted over the closed set of four names this family has ever had, so a
    revival of a dead key and a second new key are both caught by the same
    count — the failure a later split would otherwise land silently.
    """
    family = (*_LEGACY_KEYS, _LIVE_KEY)
    live = {key for key in family if _resolve_or_none(key) is not None}

    assert live == {_LIVE_KEY}, (
        f"the handoff-housekeeping family resolves {sorted(live)} live keys; "
        f"exactly {{{_LIVE_KEY!r}}} is the contract. More than one means the "
        f"job has more than one door again, which is the condition this plan "
        f"exists to end; none means the door this plan restored is off."
    )


def test_the_live_key_resolves_to_the_one_job_s_own_handler() -> None:
    """Resolution by key and the module's own function are the SAME callable.

    Without this, `test_exactly_one_housekeeping_key_resolves...` is satisfied by
    any callable at all under that name — a shim, a stub left by a patch that
    outlived its test, a second implementation. Identity is what ties the
    dispatchable name to the composition the sibling suites test.
    """
    from coordinator_core.ops import handoff_housekeeping

    assert ipc.get_op_handler(_LIVE_KEY) is handoff_housekeeping._handler


# --- (c) the reachable-leg set, asserted by call graph ----------------------


def test_only_the_one_job_reaches_the_archival_leg_directly() -> None:
    """The executable form of the redirect.

    `handoff_archive_transition._handler` survives as a library after its key
    was killed, and reaching it in-process is sanctioned — for ONE module. Every
    additional direct importer is another door onto the same compute that no
    timing gate measures, no suspension table sees, and no caller of
    `handoff.housekeeping` can account for. That is how the job came to have
    three doors before this plan, and it is not a condition a key-based
    assertion can detect, because a module reaching the leg never names the key.
    """
    offenders = []
    for path in _production_sources():
        source = path.read_text(encoding="utf-8", errors="replace")
        if not _reaches_the_leg_directly(source):
            continue
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _PERMITTED_DIRECT_IMPORTERS:
            continue
        offenders.append(rel)

    assert not offenders, (
        f"modules reaching {_LEG_MODULE}.{_LEG_ATTR} directly: "
        f"{sorted(offenders)}. Only {sorted(_PERMITTED_DIRECT_IMPORTERS)} may. "
        f"Route through {_LIVE_KEY} instead — it returns the leg's result "
        f"verbatim under 'transition', so the caller's own predicates are "
        f"unchanged (see coordinator_core/archive_stamp.py :: "
        f"_call_handoff_archive_transition for the worked example)."
    )


def test_archive_stamp_is_no_longer_a_direct_importer() -> None:
    """The one redirect this plan actually landed, pinned by name.

    `archive_stamp._call_handoff_archive_transition` imported the leg and
    `asyncio.run` it, bypassing `get_op_handler`, the suspension table and every
    gate. It was rewired on 2026-08-28. Named separately from the set assertion
    above so a revert reads as "the redirect came back out" rather than as a
    generic new-importer message that gives the reader no history.
    """
    path = _REPO_ROOT / "coordinator_core" / "archive_stamp.py"
    source = path.read_text(encoding="utf-8", errors="replace")

    assert not _reaches_the_leg_directly(source), (
        "coordinator_core/archive_stamp.py reaches "
        f"{_LEG_MODULE}.{_LEG_ATTR} directly again — the C4 redirect through "
        f"{_LIVE_KEY} has been reverted, and its three verbs (cs_ship_handoff, "
        "cs_chain_archive_handoff, cs_supersede_archive_handoff) are back to "
        "being a third door onto the one job."
    )


def test_the_permitted_importer_actually_reaches_the_leg() -> None:
    """The permitted set is a carve-out, not a historical note.

    A permitted entry that no longer reaches the leg is stale scope: the next
    module to reach it inherits an exemption nobody re-argued. Asserting the
    positive direction keeps the set exactly as wide as the code makes it.
    """
    for rel in sorted(_PERMITTED_DIRECT_IMPORTERS):
        path = _REPO_ROOT / rel
        assert path.is_file(), f"permitted importer {rel} does not exist"
        source = path.read_text(encoding="utf-8", errors="replace")
        assert _reaches_the_leg_directly(source), (
            f"{rel} is on the permitted-importer list but no longer reaches "
            f"{_LEG_MODULE}.{_LEG_ATTR}. Prune it from "
            f"_PERMITTED_DIRECT_IMPORTERS in the same commit that removed the "
            f"import — a stale exemption is scope nobody argued for."
        )
