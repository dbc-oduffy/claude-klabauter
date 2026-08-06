"""coordinator_core.bash_guards.tests.guard_message_exemptions -- the
written-reason exemption manifest for guard message-size discipline.

Spec backlink: docs/plans/2026-08-02-guard-message-size-discipline.md,
chunk C4, AC6, and the "Enriched Dispatch Stubs" C4 section.

`GUARD_MESSAGE_EXEMPTIONS` is the one and only sanctioned escape hatch from
`_message_size.MESSAGE_PROSE_CAP_BYTES` (C2): a `(guard_name, input_id)`
cell that measures `over_cap=True` and cannot reasonably be trimmed lands
here with a WRITTEN prose reason, never by quietly excluding the cell from
whichever corpus enforces the cap. Adjudicated one at a time, by the chunk
that fails to bring a specific guard/input cell under cap -- each entry
answers "why is trimming this message's alternative worse than exempting
it?". This module ships EMPTY: it is the mechanism, not a population.

NOT `coordinator_core.bash_guards._helpers`/`claude_md_budget`'s watermark
ledger. That ledger is keyed by repo-root-relative *file path*
(`surface_slug`), has zero armed consumers anywhere in this repo, and
forcing a guard-name key through it would be new-pattern invention wearing
a reuse costume -- see this plan's own Anti-scope, "Do not reuse
`claude_md_budget`'s watermark ledger for per-guard budgets."

Dead-entry enforcement, and why it is two mechanisms, not one:
`test_confinement_attack_corpus.py`'s `XFAIL_BYPASSES` gets its fail-loud
property entirely from `pytest.mark.xfail(reason=..., strict=True)` at the
cell's own parametrize site -- a bypass that gets fixed XPASSes, and a
strict XPASS is a suite failure. It has no separate "does this guard still
exist" check. `test_override_route_inventory.py`'s
`_NO_OVERRIDE_NOTE_ALLOWLIST` is the opposite shape: an explicit
`test_allowlist_entries_are_actually_registered_guards` diffs the allowlist
keys against the live guard chain, but never re-measures anything -- it has
no notion of "does this cell still meet the condition it was exempted
for." AC6 needs BOTH properties at once (a dead guard name fails, AND a
cell that no longer exceeds cap fails), so this module combines them
explicitly rather than picking one precedent and hoping it covers the
other axis:

1. `test_exemption_guards_are_currently_registered` -- structural, modeled
   on `_NO_OVERRIDE_NOTE_ALLOWLIST`'s own dead-entry test: every guard name
   in `GUARD_MESSAGE_EXEMPTIONS` must appear in
   `dispatch._build_guard_chain()`'s live output.
2. `test_exemption_cells_still_exceed_cap` -- measurement, modeled on
   `XFAIL_BYPASSES`'s strict-xfail intent but expressed as a plain
   assertion (this module has no parametrize site of its own to hang an
   `xfail` mark on): every entry must also appear in `_EXEMPTION_FIXTURES`,
   a small fixture map owned alongside the manifest itself, and the guard
   named must still measure `over_cap=True` (via `_message_size.
   measure_envelope`, C2's own measurement seam) when invoked through
   `guard_message_capture.capture_one_guard` (C1's own capture seam) with
   that fixture. An entry with no matching fixture fails loud -- a manifest
   entry that names a cell no test can reproduce is exactly the
   unverifiable parking-lot entry this module exists to prevent.

Both tests pass vacuously while the manifest is empty; they exist to fire
the moment a future chunk populates either dict.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._message_size import (
    MessageSizeMeasurement,
    measure_envelope,
)
from coordinator_core.bash_guards.tests.guard_message_capture import (
    capture_one_guard,
)

#: The exemption manifest. Keyed `(guard_name, input_id)`, value a WRITTEN
#: prose reason -- never a bare `True`/placeholder string. Ships empty; see
#: module docstring. `input_id` is a caller-chosen short label identifying
#: which corpus cell (e.g. a C3 corpus row, or a fixture in
#: `_EXEMPTION_FIXTURES` below) the exemption covers -- it is not itself
#: interpreted by this module beyond dead-entry lookup.
GUARD_MESSAGE_EXEMPTIONS: Dict[Tuple[str, str], str] = {}

#: Fixture-builders for `test_exemption_cells_still_exceed_cap`'s live
#: re-measurement, owned alongside the manifest (never imported from a
#: sibling corpus module, so this file's dead-entry enforcement never
#: depends on another chunk's shape). Keyed identically to
#: `GUARD_MESSAGE_EXEMPTIONS`; value a zero-arg callable returning
#: `(cmd, session_id, cwd, payload, host_is_windows)` -- the exact
#: positional/keyword shape `guard_message_capture.capture_one_guard`
#: takes. An entry in `GUARD_MESSAGE_EXEMPTIONS` with no matching fixture
#: here fails `test_exemption_cells_still_exceed_cap` loud, by design: a
#: reason with no reproducible cell to check is not a verifiable exemption.
_EXEMPTION_FIXTURES: Dict[Tuple[str, str], Callable[[], Tuple[str, str, str, Dict[str, Any], bool]]] = {}


def _live_guard_names() -> set:
    chain = dispatch._build_guard_chain(
        cmd="echo hi",
        session_id="test-session-guard-message-exemptions",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
        policy_file=None,
        host_is_windows=False,
    )
    return {entry.name for entry in chain}


def test_exemption_guards_are_currently_registered():
    """An exemption naming a guard that no longer exists in the chain is
    dead config -- it would silently stop covering anything without this
    check ever telling anyone. Modeled on
    `test_override_route_inventory.test_allowlist_entries_are_actually_registered_guards`."""
    live_names = _live_guard_names()
    stale = {guard_name for guard_name, _ in GUARD_MESSAGE_EXEMPTIONS if guard_name not in live_names}
    assert not stale, (
        "these exemption entries name a guard that is not currently registered in "
        "dispatch._build_guard_chain -- remove or fix them: %s" % sorted(stale)
    )


def test_exemption_cells_still_exceed_cap():
    """An exemption for a cell that no longer exceeds
    `MESSAGE_PROSE_CAP_BYTES` (the guard's own message got trimmed, or the
    input stopped triggering it) is dead config of a different shape than a
    missing guard -- it would silently keep exempting a cell that no longer
    needs exempting. Modeled on `XFAIL_BYPASSES`'s `strict=True` intent
    (a fixed bypass must be caught, not silently tolerated), expressed as a
    direct re-measurement since this module has no parametrize site to hang
    an `xfail` mark on."""
    missing_fixtures = []
    under_cap = []
    for key in GUARD_MESSAGE_EXEMPTIONS:
        guard_name, input_id = key
        builder = _EXEMPTION_FIXTURES.get(key)
        if builder is None:
            missing_fixtures.append(key)
            continue
        cmd, session_id, cwd, payload, host_is_windows = builder()
        capture = capture_one_guard(
            guard_name,
            cmd,
            session_id,
            cwd,
            payload,
            host_is_windows=host_is_windows,
        )
        measurement: MessageSizeMeasurement = measure_envelope(capture.envelope, band=capture.band)
        if not measurement.over_cap:
            under_cap.append(key)

    assert not missing_fixtures, (
        "these exemption entries have no matching _EXEMPTION_FIXTURES builder -- "
        "an exemption reason with no reproducible cell to re-measure is not "
        "verifiable: %s" % missing_fixtures
    )
    assert not under_cap, (
        "these exemption entries no longer exceed MESSAGE_PROSE_CAP_BYTES -- the "
        "guard's message must have been trimmed or the input stopped triggering "
        "it; remove the now-unneeded exemption: %s" % under_cap
    )


def test_exemption_entries_carry_a_written_reason():
    """Every value is a non-empty prose string -- guards against a future
    entry landing with a placeholder/blank reason, the exact failure mode
    the written-reason requirement exists to prevent (see module
    docstring)."""
    blank = [key for key, reason in GUARD_MESSAGE_EXEMPTIONS.items() if not reason or not reason.strip()]
    assert not blank, "these exemption entries have no written reason: %s" % blank
