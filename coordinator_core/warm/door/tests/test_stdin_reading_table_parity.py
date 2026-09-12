"""
coordinator_core.warm.door.tests.test_stdin_reading_table_parity --
the anti-staleness mechanism for `door_core.c`'s static
`door_stdin_reading_basenames` table.

Purpose: C1's derivation (`test_stdin_reading_entrypoint_population.py ::
derive_stdin_reading_entrypoints`) computes, from the resolved `.py` bodies
under the configured sibling bin roots, which allowlisted entrypoints
textually read stdin -- the SEMANTIC source of truth. `door_core.c`'s
`door_stdin_reading_basenames` array is the RUNTIME source of truth: the
door reads no file at request time, so that table is what the compiled
binary actually gates on. This test is the one hop between them, with no
allowlist JSON key as an intermediary (the earlier `stdin_reading_
entrypoints` allowlist key was cut -- grep-confirmed zero runtime
consumers, per this chunk's dispatch brief).

Both directions are asserted: a new entrypoint that reads stdin and is not
yet in the table goes red (the door would silently deliver it warm with no
stdin); a table entry that no longer reads stdin (or never did) goes red
too (the door would pay a needless cold fall-through for it). Symmetric
set equality is the whole check -- no third list is threaded through.

UNRESOLVABLE-ROOTS DISPOSITION (staff-eng finding 5, carried over from the
cut C2): when neither `repos.claude_klabauter` nor `repos.doe_claude`
resolves to an existing `coordinator/bin` on this box, this test SKIPS
with a stated reason. It never asserts empty-equals-table in that case --
that would be a false red for an environment fact unrelated to whether
`door_core.c`'s table is stale.

Spec backlink: docs/plans/2026-09-12-warm-door-drops-stdin-for-every-entrypoint.md, chunk C2
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.warm.door.tests.test_stdin_reading_entrypoint_population import (
    configured_bin_roots,
    derive_stdin_reading_entrypoints,
)

_DOOR_CORE_C_PATH = Path(__file__).resolve().parents[1] / "door_core.c"

#: Matches the static table's braced initializer body in `door_core.c`.
#: Deliberately anchored on the exact spelling `door_stdin_reading_
#: basenames` -- this plan's falsifier greps for that name, and so does
#: this test: a rename that drops the name silently stops this parity
#: check from finding the table at all, which this regex turns into a
#: loud parse failure below rather than a silent empty-table false green.
_TABLE_DECL_RE = re.compile(
    r"door_stdin_reading_basenames\[\]\s*=\s*\{(?P<body>.*?)\};",
    re.DOTALL,
)

#: Matches one double-quoted C string literal entry in the table body.
_STRING_LITERAL_RE = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')


def parse_door_core_table(source: str) -> "frozenset[str]":
    """The exact set of basenames `door_core.c`'s static table declares --
    parsed textually (this is a `.c` file, not Python), not executed. Raises
    `AssertionError` if the table's declaration cannot be found at all,
    rather than returning an empty set that would read as "the table is
    empty" instead of "this parser could not find the table"."""
    match = _TABLE_DECL_RE.search(source)
    assert match is not None, (
        "could not find 'door_stdin_reading_basenames[] = { ... };' in "
        f"{_DOOR_CORE_C_PATH} -- table renamed, reshaped, or removed"
    )
    body = match.group("body")
    return frozenset(_STRING_LITERAL_RE.findall(body))


@pytest.mark.real_home
def test_door_core_table_matches_derivation_exactly():
    """The static table `door_core.c` hardcodes equals C1's derivation over
    the live resolved `.py` bodies, in both directions. See module
    docstring for what a mismatch in either direction means at runtime."""
    if not configured_bin_roots():
        pytest.skip(
            "neither repos.claude_klabauter nor repos.doe_claude resolves to an "
            "existing coordinator/bin on this machine -- cannot compute the "
            "semantic side of this parity check"
        )

    derived = derive_stdin_reading_entrypoints()
    table = parse_door_core_table(_DOOR_CORE_C_PATH.read_text(encoding="utf-8"))

    missing_from_table = derived - table
    stale_in_table = table - derived

    assert not missing_from_table, (
        "entrypoint(s) read stdin per C1's derivation but are absent from "
        f"door_core.c's door_stdin_reading_basenames table: {sorted(missing_from_table)} "
        "-- the door would deliver these warm into a pool worker whose "
        "sys.stdin is None"
    )
    assert not stale_in_table, (
        "door_core.c's door_stdin_reading_basenames table names entrypoint(s) "
        f"C1's derivation no longer finds reading stdin: {sorted(stale_in_table)} "
        "-- the door pays a needless cold fall-through for these"
    )
