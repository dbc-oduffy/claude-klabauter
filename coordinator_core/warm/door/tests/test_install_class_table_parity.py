"""
coordinator_core.warm.door.tests.test_install_class_table_parity --
the anti-staleness mechanism for `door_core.c`'s static
`door_install_class_basenames` table.

Purpose: PM ruling 2026-09-23 ("install shouldn't route via the warm engine,
because it installs") is enforced at RUNTIME by `door_core.c`'s
`door_install_class_basenames` array -- the door reads no file at request
time, so that table is what the compiled binary actually gates on. The
SEMANTIC source of truth is each CLI's own `INSTALL_CLASS = True` module-level
declaration in `coordinator/bin/<name>.py`
(`coordinator_core.install.door_install.declared_install_class`, the same
oracle `test_install_class_runs_cold.py` already builds its own roster from).
This test is the one hop between them, mirroring
`test_stdin_reading_table_parity.py`'s own shape for C2's table exactly.

Both directions are asserted: a CLI that declares `INSTALL_CLASS = True` but
is absent from the table would still get dialled to the warm engine it is
replacing; a table entry whose CLI no longer declares `INSTALL_CLASS = True`
(or never did, or was removed) pays a needless cold fall-through for a name
that no longer needs one. Symmetric set equality is the whole check.

Spec backlink: dispatch brief, item 2 (cold-only gate for install-class
names), 2026-09-23.
"""
from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.install import door_install

_DOOR_DIR = Path(__file__).resolve().parents[1]
_DOOR_CORE_C_PATH = _DOOR_DIR / "door_core.c"

#: Matches the static table's braced initializer body in `door_core.c`.
#: Deliberately anchored on the exact spelling `door_install_class_
#: basenames` -- this table's falsifier greps for that name, and so does
#: this test.
_TABLE_DECL_RE = re.compile(
    r"door_install_class_basenames\[\]\s*=\s*\{(?P<body>.*?)\};",
    re.DOTALL,
)

_STRING_LITERAL_RE = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')


def parse_door_core_table(source: str) -> "frozenset[str]":
    """The exact set of basenames `door_core.c`'s static
    `door_install_class_basenames` table declares -- parsed textually (this
    is a `.c` file, not Python), not executed. Raises `AssertionError` if the
    table's declaration cannot be found at all, rather than returning an
    empty set that would read as "the table is empty" instead of "this
    parser could not find the table"."""
    match = _TABLE_DECL_RE.search(source)
    assert match is not None, (
        "could not find 'door_install_class_basenames[] = { ... };' in "
        f"{_DOOR_CORE_C_PATH} -- table renamed, reshaped, or removed"
    )
    body = match.group("body")
    return frozenset(_STRING_LITERAL_RE.findall(body))


def derive_install_class_basenames() -> "frozenset[str]":
    """Every `coordinator/bin/*.py` CLI whose own module body declares
    `INSTALL_CLASS = True` -- the semantic source of truth, built from the
    same oracle `test_install_class_runs_cold.py` uses
    (`door_install.declared_install_class`), over the same local
    `coordinator/bin` this repo carries (`door_install._GENERATOR_BIN_DIR`),
    not a sibling working tree."""
    bin_dir = door_install._GENERATOR_BIN_DIR
    return frozenset(
        p.stem
        for p in bin_dir.glob("*.py")
        if door_install.declared_install_class(p.stem) is True
    )


def test_door_core_table_matches_declared_install_class_exactly():
    """The static table `door_core.c` hardcodes equals the live
    `INSTALL_CLASS = True` declarations in `coordinator/bin`, in both
    directions. See module docstring for what a mismatch in either direction
    means at runtime."""
    derived = derive_install_class_basenames()
    table = parse_door_core_table(_DOOR_CORE_C_PATH.read_text(encoding="utf-8"))

    missing_from_table = derived - table
    stale_in_table = table - derived

    assert not missing_from_table, (
        "CLI(s) declare INSTALL_CLASS = True but are absent from door_core.c's "
        f"door_install_class_basenames table: {sorted(missing_from_table)} -- "
        "the door would dial the warm engine these are meant to never reach"
    )
    assert not stale_in_table, (
        "door_core.c's door_install_class_basenames table names basename(s) "
        f"that no longer declare INSTALL_CLASS = True: {sorted(stale_in_table)} "
        "-- the door pays a needless cold fall-through for these"
    )
