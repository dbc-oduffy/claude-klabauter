"""Tests of the write see the decision JSON, not the engine-ask submission line.

A run-protocol hook, not an autouse fixture: a test selected by node id beside whole-file
arguments from other directories is collected without this directory's autouse fixtures,
and the real hand-off then printed its `Workflow(...)` line over the decision JSON.
`test_plan_hand_off.py` exercises the real hand-off.
"""

import json

import pytest

import coordinator_core.sizing_assemble as sizing_assemble


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if item.module.__name__.endswith("test_plan_hand_off"):
        yield
        return

    def _print_decision(_write_path, decision):
        print(json.dumps(decision, indent=2, sort_keys=True))
        return sizing_assemble.EXIT_OK

    patch = pytest.MonkeyPatch()
    patch.setattr(sizing_assemble, "_hand_to_engine_ask", _print_decision)
    try:
        yield
    finally:
        patch.undo()
