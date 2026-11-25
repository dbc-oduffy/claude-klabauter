"""A refire's resume script stays under the shared ask ceiling however many revise notes it carries.

Production form: the m_plus chain-hop launcher child with an in-run commit, `staged_out` on, hoisting on, the
installed plan-blitz grown to the padded size, four halted predecessors and `restore_evicted_run` as a refire emits.
Notes are pairwise distinct, so the hoister cannot dedupe them away.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.benchmarks.ask_script_bytes_probe import REFIRE_PRIOR_RUN_IDS, refire_notes
from coordinator_core.ops.dispatch_emit import ask_compose, emit, prompt_fragments
from coordinator_core.ops.dispatch_emit.compose_size import ASK_SCRIPT_CEILING_BYTES
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_script_size_budget import _SIZING_REL, _write_sizing
from coordinator_core.ops.dispatch_emit.tests.test_ask_script_byte_headroom import _PLUGIN_ROOT, _padded_plan_blitz_text

_PER_NOTE_LIMIT = 1024


@pytest.fixture
def _production_inputs(monkeypatch):
    assert _PLUGIN_ROOT, "plugin root did not resolve at import; plan-blitz.mjs is unreachable"
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", _PLUGIN_ROOT)
    monkeypatch.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)


def _refire_script(root: Path, notes: int) -> tuple[str, dict]:
    _write_sizing(root)
    sink: dict = {}
    script = ask_compose.compose_ask_script(
        repo_root=str(root), prompt=None, sizing_rel=_SIZING_REL, run_id="run-1", session_id=None,
        script_path="scratch/warp/x.mjs", plan_blitz_text=_padded_plan_blitz_text(), staged_out=sink,
        launcher_child=True, chain_hop=True, in_run_commit=True, restore_evicted_run=True,
        carried_notes=refire_notes(notes), prior_run_ids=REFIRE_PRIOR_RUN_IDS,
    )
    return script, sink


def test_refire_with_one_and_with_24_notes_is_under_the_ceiling(tmp_path, _production_inputs):
    for n in (1, 24):
        script, _ = _refire_script(tmp_path / f"n{n}", n)
        size = len(script.encode("utf-8"))
        assert size <= ASK_SCRIPT_CEILING_BYTES, f"refire with {n} notes is {size} bytes; ceiling {ASK_SCRIPT_CEILING_BYTES}"


def test_each_added_note_grows_the_script_under_the_per_note_limit(tmp_path, _production_inputs):
    one, _ = _refire_script(tmp_path / "one", 1)
    many, _ = _refire_script(tmp_path / "many", 24)
    per_note = (len(many.encode("utf-8")) - len(one.encode("utf-8"))) / 23
    assert per_note < _PER_NOTE_LIMIT, f"each carried note adds {per_note:.0f} script bytes"


def test_the_notes_text_reaches_the_staged_prompt_not_the_script(tmp_path, _production_inputs):
    script, sink = _refire_script(tmp_path, 24)
    last = refire_notes(24)[-1]["note"]
    assert last not in script
    assert last in sink["revise-notes"]
    assert "revise-notes.md" in script


def test_a_refire_with_few_small_notes_keeps_them_inline(tmp_path, _production_inputs):
    _write_sizing(tmp_path)
    sink: dict = {}
    note = {"id": "N1", "note": "keep the ask", "at": "2026-11-25T21:00:09Z", "run": "r0", "retracted": None}
    script = ask_compose.compose_ask_script(
        repo_root=str(tmp_path), prompt=None, sizing_rel=_SIZING_REL, run_id="run-1", session_id=None,
        script_path="scratch/warp/x.mjs", plan_blitz_text=_padded_plan_blitz_text(), staged_out=sink,
        launcher_child=True, chain_hop=True, in_run_commit=True, carried_notes=[note],
    )
    assert "keep the ask" in script
    assert "revise-notes" not in sink
