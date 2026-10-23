"""Guard: the dispatch_emit/tests autouse isolation attaches in any argument order.

A Package-level conftest's fixtures detached from a dispatch_emit/tests file collected after an
unrelated ops/ file; the fixtures now live in coordinator_core/conftest.py.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_OPS = Path(__file__).resolve().parents[2]
_TESTS = Path(__file__).resolve().parent
_FIXTURES = (
    "_no_ambient_plugin_root",
    "_unhoisted_ask_scripts",
    "_no_ambient_box_terms",
    "_write_set_reads_clean",
)


def test_isolation_fixtures_are_active_in_this_file():
    from coordinator_core.ops.dispatch_emit import prompt_fragments

    assert prompt_fragments.HOIST_ENABLED is False, "dispatch_emit autouse fixtures detached"
    assert "CLAUDE_PLUGIN_ROOT" not in os.environ, "dispatch_emit autouse fixtures detached"


def test_interleaved_collection_keeps_the_fixtures_on_the_later_file(pytester):
    args = [
        str(_TESTS / "test_xs_one_row_leg_budget.py"),
        str(_OPS / "test_blocked.py"),
        str(_TESTS / "test_ask_compose_fanout_groups.py"),
    ]
    result = pytester.runpytest_inprocess("--setup-plan", "-p", "no:cacheprovider", *args)
    out = result.stdout.str()
    later = [
        ln for ln in out.splitlines() if "test_ask_compose_fanout_groups.py::" in ln
    ]
    assert later, out[-2000:]
    for ln in later:
        missing = [f for f in _FIXTURES if f not in ln]
        assert not missing, f"detached after an interleaved ops/ file: {missing} on {ln[:120]}"
