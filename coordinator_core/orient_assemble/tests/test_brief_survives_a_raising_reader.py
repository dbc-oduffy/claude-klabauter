"""
coordinator_core.orient_assemble.tests.test_brief_survives_a_raising_reader —
a reader family that raises costs its own contribution to the brief, never
the whole orientation.

This property used to be asserted inside each reader, which is why the
readers grew bare `except Exception` clauses: with no guard in `brief()`'s
loop, going quiet was the only way a reader could avoid killing the session's
orientation. Those clauses then swallowed the readers' own defects — a
`ModuleNotFoundError` from a sibling bootstrap read as a clean box, because a
clause wide enough to keep orientation alive cannot tell a vanishing file
apart from a broken import.

The guard now lives at the loop, so this file is where the property is
pinned. A reader's own `except` is free to name only what it expects.
"""

from __future__ import annotations

from types import ModuleType

import pytest

from coordinator_core import orient_assemble
from coordinator_core.orient_assemble.reader_result import ReaderResult


def _reader_module(name, collect):
    """Stub in the shape `_READER_MODULES` actually holds -- a module, not a
    class. A class would report its own bare `__name__`, so the assembler's
    "which reader degraded" line would pass here while naming something
    unlike any real reader."""
    mod = ModuleType(f"coordinator_core.orient_assemble.{name}")
    mod.collect = collect
    return mod


def _raises(cadence, *, repo_root=None):
    raise RuntimeError("reader blew up")


def _works(cadence, *, repo_root=None):
    return ReaderResult(
        directives=[
            {
                "id": "d-still-here",
                "cli": "noop",
                "args": [],
                "depends_on": None,
                "already_satisfied": False,
                "detail": "surviving reader's directive",
            }
        ]
    )


_EXPLODING = _reader_module("readers_exploding", _raises)
_FINE = _reader_module("readers_fine", _works)


def test_a_raising_reader_does_not_kill_the_brief(monkeypatch, capsys):
    """The brief is still produced, and the surviving reader's directive is
    still in it."""
    monkeypatch.setattr(
        orient_assemble, "_READER_MODULES", (_EXPLODING, _FINE)
    )

    brief = orient_assemble.brief("day")

    ids = [d["id"] for d in brief["directives"]]
    assert "d-still-here" in ids


def test_the_degraded_reader_is_named_on_stderr(monkeypatch, capsys):
    """A dropped reader must be visible. Silently returning a short brief
    would report a degraded orientation as a clean one — the exact failure
    the per-reader clauses produced."""
    monkeypatch.setattr(
        orient_assemble, "_READER_MODULES", (_EXPLODING, _FINE)
    )

    orient_assemble.brief("day")

    err = capsys.readouterr().err
    assert "readers_exploding" in err
    assert "RuntimeError" in err
    assert "reader blew up" in err
