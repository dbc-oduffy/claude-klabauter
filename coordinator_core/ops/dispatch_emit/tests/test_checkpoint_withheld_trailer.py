"""Checkpoint-Withheld trailer: renderer, parser, and the stamp skipping a row whose declared path it names."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import checkpoint_stamp as cs

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_PLAN = "docs/plans/p.md"


def test_renderer_is_sorted_forward_slash_and_distinct() -> None:
    lines = cs.checkpoint_withheld_trailer_lines(["b\\y.py", "a/x.py", "./a/x.py", "", "  "])
    assert lines == ["Checkpoint-Withheld: a/x.py", "Checkpoint-Withheld: b/y.py"]
    assert cs.CHECKPOINT_WITHHELD_TRAILER == "Checkpoint-Withheld"


def test_parser_round_trips_and_ignores_other_lines() -> None:
    body = "checkpoint(wave 0): 1 row — C1\n\nCheckpoint-Plan: p.md\n" + "\n".join(
        cs.checkpoint_withheld_trailer_lines(["a/x.py", "src/with space.py"])
    )
    assert cs.checkpoint_withheld(body) == frozenset({"a/x.py", "src/with space.py"})
    assert cs.checkpoint_withheld("no trailer") == frozenset()
    assert cs.checkpoint_withheld("Checkpoint-Withheld:\n") == frozenset()
