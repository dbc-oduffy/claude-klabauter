"""dispatch.ask_stage on a 26-row plan in a 253-dirty-file repo: `stage()` and `_handler` each stay under the 500ms bar."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.tests._size_l_plan import (
    RELAYED_DIRTY_FILES,
    TWENTY_SIX_ROWS,
    build_size_l_plan,
    isolate_launch_dirs,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

BAR_MS = 500.0
SESSION_ID = "26-row-budget-session"


def _fixture(tmp_path, monkeypatch):
    isolate_launch_dirs(monkeypatch, tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    return build_size_l_plan(
        root, rows=TWENTY_SIX_ROWS, dirty_files=RELAYED_DIRTY_FILES, session_id=SESSION_ID
    )


def test_26_row_dirty_stage_under_the_brightline(tmp_path, monkeypatch):
    repo_root, plan_rel, run_id = _fixture(tmp_path, monkeypatch)

    def call():
        return ask_stage.stage(repo_root, run_id=run_id, plan_rel=plan_rel, session_id=SESSION_ID)

    timing = in_process_time_ms(call)
    assert timing["process_time_ms"] < BAR_MS, f"26-row dirty stage took {timing['process_time_ms']}ms process time"
    assert call().rows, "stage returned no rows: the figure above measured nothing"


def test_26_row_dirty_handler_under_the_brightline(tmp_path, monkeypatch):
    repo_root, plan_rel, run_id = _fixture(tmp_path, monkeypatch)
    params = {"run_id": run_id, "plan_path": plan_rel, "session_id": SESSION_ID}

    replies = []

    def call():
        reply = ask_stage._handler(params, repo_root=repo_root)
        replies.append(reply)
        return reply

    timing = in_process_time_ms(call)
    assert "manifest_handle" in replies[0], f"handler did not stage: {replies[0]}"
    assert timing["process_time_ms"] < BAR_MS, f"26-row dirty handler took {timing['process_time_ms']}ms process time"
