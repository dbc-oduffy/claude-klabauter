"""An emitted warp-ask script builds its checkpoint trailer at runtime and refuses to execute without it."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose


def _message_line(script: str) -> str:
    m = re.search(r"^\s*const message = .*$", script, re.M)
    assert m, "ask script has no checkpoint message expression"
    return m.group(0)


def _pre_execute(script: str) -> str:
    return script.split("phase('Execute')", 1)[0]


def test_ask_checkpoint_message_carries_both_trailer_lines():
    line = _message_line(_compose())
    assert "Checkpoint-Plan: " in line
    assert "Checkpoint-Base: " in line


def test_ask_trailer_names_no_baked_plan_or_sha():
    line = _message_line(_compose())
    assert not re.search(r"[0-9a-f]{40}", line)
    assert "docs/plans/" not in line


def test_ask_run_base_comes_from_the_staged_manifest_not_an_agent():
    head = _pre_execute(_compose())
    assert "_runBase = /^[0-9a-f]{40}$/.test(String(_manifest?.head_sha ?? ''))" in head
    assert "label: 'run-base'" not in head


def test_ask_refuses_to_enter_execution_without_plan_or_run_base():
    head = _pre_execute(_compose())
    tail = head[head.rindex("phase('plan')"):]
    assert "!_runBase" in tail and "_halted = {" in tail
    assert "Checkpoint-Plan" in tail and "Checkpoint-Base" in tail, (
        "the refusal before Execute must state the missing trailer input by name"
    )
