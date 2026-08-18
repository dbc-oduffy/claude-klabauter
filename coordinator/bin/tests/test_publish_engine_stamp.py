"""
coordinator.bin.tests.test_publish_engine_stamp

Pins the publisher's engine-build-stamp contract to the engine's own reader.

`publish.py` writes `coordinator_core/.engine-stamp` into the engine row's
restricted tree; `coordinator_core.warm.skew.compute_client_token` reads it and
derives the warm engine's generation token from its bytes. The two live in
different processes with no shared import -- publish.py runs standalone against
a `percolate` lib path and deliberately does not import the package it
publishes -- so the filename is duplicated on both sides.

A silent divergence here does not fail loudly. It degrades: the published
engine simply stops carrying a stamp the reader recognises, the token falls
back to the git-ref fingerprint, and the warm engine returns to rotating its
generation every ~32 seconds on a shared branch (measured 2026-08-18) while
every test elsewhere stays green. That is the failure this test exists to make
impossible.
"""

import re
from pathlib import Path

from coordinator_core.warm import skew

_PUBLISH_PY = Path(__file__).resolve().parents[1] / "publish.py"


def _publisher_stamp_filename() -> str:
    """Read the publisher's constant textually rather than by import --
    importing `publish.py` executes its module-level `percolate` imports,
    which resolve only under the script's own sys.path bootstrap."""
    src = _PUBLISH_PY.read_text(encoding="utf-8")
    match = re.search(r'^_ENGINE_STAMP_FILENAME\s*=\s*"([^"]+)"', src, re.MULTILINE)
    assert match is not None, "publish.py no longer defines _ENGINE_STAMP_FILENAME"
    return match.group(1)


def test_publisher_and_engine_agree_on_the_stamp_filename():
    assert _publisher_stamp_filename() == skew.ENGINE_STAMP_FILENAME, (
        "publish.py writes a stamp filename the engine's own reader does not "
        "look for -- the published engine would silently fall back to the "
        "git-ref token and resume rotating its generation on every peer commit"
    )


def test_publisher_writes_the_stamp_only_for_the_engine_package_row():
    """Scoped by the same predicate as the import-closure gate. A stamp in a
    `bin/` or `lib/` row would name a generation nothing reads."""
    src = _PUBLISH_PY.read_text(encoding="utf-8")
    assert "if target.source_dir.name == _CLOSURE_PACKAGE_NAME:" in src
    assert "_ENGINE_STAMP_FILENAME" in src


def test_stamp_is_written_after_the_gates_not_before():
    """A refused round must not ship a stamp. Pins ordering: the stamp write
    appears AFTER the import-closure gate's failure return in the source."""
    src = _PUBLISH_PY.read_text(encoding="utf-8")
    closure_fail = src.index("import-closure violation(s) in")
    stamp_write = src.index("Engine build stamp:")
    assert closure_fail < stamp_write, (
        "the engine stamp is written before the closure gate can refuse the "
        "row -- a failing round would ship a stamp naming a build it rejected"
    )
