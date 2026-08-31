"""`is_provably_undispatched` is ONE function, shared verbatim by both doors.

Spec backlink: docs/plans/2026-08-31-the-settings-home-crosses-the-warm-boundary.md § C2
(folded in from what was C5 -- overengineering review found a comment-prose fix did not
need its own chunk, but the classification claim itself still needs a test that names
BOTH transport legs, not just the one this box happens to run.)

WHY BOTH LEGS, NOT ONE. `door.c` (Windows named pipe) and `door_posix.c` (POSIX unix
socket) each `#include "door_core.h"` and link against the SAME `door_core.c` translation
unit -- neither file defines its own copy of `is_provably_undispatched`, and both cite the
shared one in their own header comments (see `door.c`'s "the single exception ... shared
with the POSIX door" and `door_posix.c`'s mirrored note). A test that only builds and runs
`door.exe` (this box's own platform) and asserts -32008 falls through there has checked
Windows's classification, not the CLAIM that the two transports agree -- the second half
is a source-level fact about which .c file the constant lives in, not a fact either
platform's binary alone can demonstrate.

WHAT THIS FILE DOES NOT DO. It does not compile or run `door_core_selftest.c` (that binary
already pins the classification list itself, per `door_core_selftest.c`'s own role,
README-posix.md's "run this first" note) and it does not spawn either door binary
(`test_door_read_deadline.py` / `test_door_read_deadline_posix.py` already do that,
platform-gated). This is the narrower, cheap, no-binary-needed check this chunk's own body
asks for: that both `.c` files reach for the classification through the ONE shared header
symbol, that neither shadows or redefines it locally, and that -32008 is on the shared
list -- so a future reader of either door's own comments does not have to trust prose
alone. Runs on every OS this test suite imports on; nothing here needs `door.exe` on disk.
"""

from __future__ import annotations

import re
from pathlib import Path

_DOOR_DIR = Path(__file__).resolve().parents[1] / "door"
_DOOR_CORE_H = _DOOR_DIR / "door_core.h"
_DOOR_CORE_C = _DOOR_DIR / "door_core.c"
_DOOR_WINDOWS_C = _DOOR_DIR / "door.c"
_DOOR_POSIX_C = _DOOR_DIR / "door_posix.c"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_settings_home_mismatch_is_on_the_shared_classification_list():
    """-32008 is one of the codes `is_provably_undispatched` recognises --
    read out of `door_core.c` itself, not pinned as a second literal that
    can drift away from the source it is about."""
    source = _read(_DOOR_CORE_C)
    match = re.search(
        r"int is_provably_undispatched\(long code\)\s*\{(.*?)\n\}",
        source,
        re.DOTALL,
    )
    assert match, "is_provably_undispatched not found in door_core.c -- renamed or moved"
    body = match.group(1)
    assert "JSONRPC_SETTINGS_HOME_MISMATCH" in body


def test_both_doors_include_the_shared_header_and_neither_redefines_the_function():
    """Both transport legs reach the classification through the SAME symbol.

    A door that `#define`s or re-declares `is_provably_undispatched` itself
    (rather than pulling the one in `door_core.h`/`door_core.c`) would let the
    two legs' classifications drift silently -- exactly the failure mode this
    chunk's own body names as the reason a Windows-only test "has not checked
    the shared claim."
    """
    for source_path in (_DOOR_WINDOWS_C, _DOOR_POSIX_C):
        source = _read(source_path)
        assert '#include "door_core.h"' in source, (
            f"{source_path.name} does not include door_core.h -- it cannot be "
            "sharing is_provably_undispatched with the other transport"
        )
        # Neither door defines its own body for the function -- only door_core.c
        # may (the `int is_provably_undispatched(long code) {` shape with a body).
        assert not re.search(
            r"int\s+is_provably_undispatched\s*\([^)]*\)\s*\{", source
        ), f"{source_path.name} defines its own is_provably_undispatched -- the doors have drifted"


def test_settings_home_mismatch_constant_is_defined_once():
    """`JSONRPC_SETTINGS_HOME_MISMATCH` has exactly one `#define`, in the
    shared header -- a second definition in either door's own file would be
    how the two legs silently disagree on the code's numeric value."""
    header_source = _read(_DOOR_CORE_H)
    assert header_source.count("#define JSONRPC_SETTINGS_HOME_MISMATCH") == 1

    for source_path in (_DOOR_WINDOWS_C, _DOOR_POSIX_C, _DOOR_CORE_C):
        source = _read(source_path)
        assert "#define JSONRPC_SETTINGS_HOME_MISMATCH" not in source, (
            f"{source_path.name} redefines JSONRPC_SETTINGS_HOME_MISMATCH -- "
            "it must come from door_core.h alone"
        )
