"""The m_plus compositions production emits, parent and chain-hop child, stay under the runner cap with a grown plan-blitz.

Both are measured staged (`staged_out`, as `dispatch.emit` always passes) over a known m_plus sizing: that shape
carries the plan-blitz block and no in-run size stage. The unsized `_known_arm`-patched fixture in
test_ask_script_byte_headroom composes a script production never emits, so it is not the m_plus measure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, emit, prompt_fragments
from coordinator_core.ops.dispatch_emit.compose_size import ASK_SCRIPT_CEILING_BYTES
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_script_size_budget import _SIZING_REL, _write_sizing
from coordinator_core.ops.dispatch_emit.tests.test_ask_script_byte_headroom import _PLUGIN_ROOT, _padded_plan_blitz_text

_LIMIT = ASK_SCRIPT_CEILING_BYTES


@pytest.fixture
def _production_inputs(monkeypatch):
    assert _PLUGIN_ROOT, "plugin root did not resolve at import; plan-blitz.mjs is unreachable"
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", _PLUGIN_ROOT)
    monkeypatch.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)


def staged_m_plus(root: Path, **shape) -> tuple[str, dict]:
    """The production m_plus script (known sizing, `staged_out` sink) over the 279005-byte padded plan-blitz."""
    _write_sizing(root)
    assert ask_compose._known_arm(str(root), _SIZING_REL) == "m_plus"
    sink: dict = {}
    script = ask_compose.compose_ask_script(
        repo_root=str(root), prompt=None, sizing_rel=_SIZING_REL, run_id="run-1", session_id=None,
        script_path="scratch/warp/x.mjs", plan_blitz_text=_padded_plan_blitz_text(), staged_out=sink, **shape,
    )
    return script, sink


def test_a_known_m_plus_parent_carries_plan_blitz_under_the_cap(tmp_path, _production_inputs):
    script, sink = staged_m_plus(tmp_path)
    n = len(script.encode("utf-8"))
    assert sink, "the stager is on in production: a staged script holds at least one prompt pointer"
    assert "async function planBlitz(" in script
    assert n <= _LIMIT, f"m_plus parent is {n} bytes; limit is {_LIMIT} (shared ask ceiling)"


def test_an_m_plus_chain_hop_child_carries_plan_blitz_under_the_cap(tmp_path, _production_inputs):
    script, _ = staged_m_plus(tmp_path, launcher_child=True, chain_hop=True, in_run_commit=True)
    n = len(script.encode("utf-8"))
    assert "async function planBlitz(" in script
    assert n <= _LIMIT, f"m_plus chain child is {n} bytes; limit is {_LIMIT} (shared ask ceiling)"
