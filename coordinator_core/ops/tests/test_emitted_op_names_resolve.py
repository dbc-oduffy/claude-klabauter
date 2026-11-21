"""Every `coordinator-invoke <op.name>` literal a shipped `coordinator/` or `coordinator_core/` source, template, skill or fixture emits must name a registered op.

Half (a) of the emitted-op-name guard; half (b) is the roster-resolution refusal in
`review_mint/roster.py`, which covers the names composed at runtime (`{agent.op}`).
"""
from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.tests.test_doctrine_prose_op_names_resolve import _CALL, _live_op_names

REPO_ROOT = Path(__file__).resolve().parents[3]
_SCANNED = ("coordinator", "coordinator_core")
_SUFFIXES = (".py", ".mjs", ".md", ".sh", ".json")
_TEST_PART = re.compile(r"(^|/)(tests?|testing)/|(^|/)(test_[^/]*|conftest)\.py$")


def unresolved_names(text: str, registered=None) -> "list[str]":
    """Dotted op names `text` invokes that are not registered, sorted, deduplicated.

    Matched per line, as the doctrine-prose guard does, so a quoted `"coordinator-invoke"`
    at a line end never swallows the next line's token; a `{placeholder}` never matches."""
    live = _live_op_names() if registered is None else registered
    return sorted({name for line in text.splitlines() for name in _CALL.findall(line) if name not in live})


def test_a_synthetic_unregistered_op_is_caught():
    assert unresolved_names("run coordinator-invoke nope.missing '{}' --bare") == ["nope.missing"]


def test_a_registered_op_and_a_placeholder_pass():
    assert unresolved_names("coordinator-invoke {agent.op} x; coordinator-invoke test_verdict.record") == []
