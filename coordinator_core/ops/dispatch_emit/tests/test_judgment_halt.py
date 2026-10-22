"""judgment_halt: string-shape and (when node exists) syntax checks over the emitted JS."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_fanout_contract import FANOUT_OP
from coordinator_core.ops.dispatch_emit.judgment_halt import pull_split_prompt, rule_and_refire_next_action_js

_NEXT = rule_and_refire_next_action_js("_planRel", "_rulings", "'REFIRE'")
_PROMPT = pull_split_prompt("_planRel", "_sizingRel")


def test_next_action_carries_every_pinned_key():
    for key in ("kind: 'rule_and_refire'", "plan: _planRel", "rulings: _rulings", "revise:", "refire: 'REFIRE'"):
        assert key in _NEXT


def test_revise_names_workflow_revise_and_one_note_per_ruling_id():
    assert "workflow.revise" in _NEXT
    assert "note_per_ruling: (_rulings).map((r) => r.id)" in _NEXT
    assert "J<n>" in _NEXT


def test_prompt_names_fanout_op_confirm_split_and_slice_shape():
    assert FANOUT_OP in _PROMPT
    assert "confirm_split" in _PROMPT
    assert "Return pick " in _PROMPT and "split" in _PROMPT and "{label, ask, rows, writes?}" in _PROMPT
    assert "_planRel" in _PROMPT and "_sizingRel" in _PROMPT


def test_prompt_forbids_deleting_rows():
    assert "Delete no row" in _PROMPT


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_emitted_js_parses_and_evaluates(tmp_path):
    script = tmp_path / "t.js"
    script.write_text(
        "const args = {confirm_split: false}; const _planRel='p.md'; const _sizingRel='s.yaml';\n"
        "const _rulings=[{id:'J1'},{id:'J2'}];\n"
        f"const a = {_NEXT}; const p = {_PROMPT};\n"
        "if (a.revise.note_per_ruling.join() !== 'J1,J2' || !a.revise.call.includes('J1, J2')) throw new Error('revise');\n"
        "if (!p.includes('p.md') || !p.includes('s.yaml')) throw new Error('prompt');\n"
    )
    done = subprocess.run(["node", str(script)], capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert done.returncode == 0, done.stderr
