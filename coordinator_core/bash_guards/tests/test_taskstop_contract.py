"""Pins the TaskStop reaper contract: names, outcomes, protocol, and import closure."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from coordinator_core.bash_guards import _taskstop_contract as c

_ALLOWED_INTERNAL = {"coordinator_core.bash_guards._heavy_admission_contract"}


def test_op_names_pinned():
    assert c.RECORDER_OP == "hooks.taskstop_launch_recorder"
    assert c.REAPER_OP == "hooks.taskstop_reaper"


def test_outcome_values_pinned():
    assert {o.value for o in c.ReapOutcome} == {
        "killed", "incomplete", "ambiguous", "no-record", "session-mismatch", "no-candidate",
    }


def test_ttl_exceeds_a_day_and_relpaths_are_relative():
    assert c.RECORD_TTL_S > 24 * 3600
    for rel in (c.STORE_RELPATH, c.LOG_RELPATH):
        assert not Path(rel).is_absolute()


def test_reap_row_defaults():
    row = c.ReapRow(task_id="t", outcome=c.ReapOutcome.NO_RECORD)
    assert row.killed == () and row.competing == () and row.candidates == 0


def test_kill_primitives_protocol_is_structural():
    class Stub:
        def terminate_verified(self, pid, ctime):
            return False

        def command_line(self, pid, ctime):
            return None

    assert isinstance(Stub(), c.KillPrimitives)
    assert not isinstance(object(), c.KillPrimitives)


def test_import_closure_is_stdlib_plus_heavy_admission_contract():
    tree = ast.parse(Path(c.__file__).read_text(encoding="utf-8"))
    stdlib = set(sys.stdlib_module_names)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [node.module or ""]
        else:
            continue
        for m in mods:
            assert m.split(".")[0] in stdlib or m in _ALLOWED_INTERNAL, m
