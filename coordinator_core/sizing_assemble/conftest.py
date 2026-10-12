import json

import pytest

import coordinator_core.sizing_assemble as sizing_assemble


@pytest.fixture(autouse=True)
def _no_plan_workflow_hand_off(request, monkeypatch):
    """The `--write` hand-off to emit-wave-fire needs an installed plugin root; tests of the
    write itself see the decision JSON instead. `test_plan_hand_off.py` exercises the hand-off."""
    if request.module.__name__.endswith("test_plan_hand_off"):
        return

    def _print_decision(_write_path, decision):
        print(json.dumps(decision, indent=2, sort_keys=True))
        return sizing_assemble.EXIT_OK

    monkeypatch.setattr(sizing_assemble, "_hand_to_plan_workflow", _print_decision)
