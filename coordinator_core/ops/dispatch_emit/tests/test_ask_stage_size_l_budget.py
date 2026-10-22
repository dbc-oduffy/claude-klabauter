"""dispatch.ask_stage on the size-L fixture: stage() (not the handler's receipt and plan-stamp legs) stays under the 500ms bar."""

from __future__ import annotations

from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.ops.dispatch_emit import ask_stage, live_run_writes
from coordinator_core.ops.dispatch_emit.tests._size_l_plan import build_size_l_plan, isolate_launch_dirs

BAR_MS = 500.0


def test_size_l_stage_under_the_brightline(tmp_path, monkeypatch):
    isolate_launch_dirs(monkeypatch, tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    repo_root, plan_rel, run_id = build_size_l_plan(root)

    def call():
        return ask_stage.stage(repo_root, run_id=run_id, plan_rel=plan_rel)

    assert call().rows, "stage returned no rows: the figure below would measure nothing"
    ms = in_process_time_ms(call)["process_time_ms"]
    assert ms < BAR_MS, f"size-L stage took {ms}ms process time"


def test_overlap_scan_over_large_footprints_stays_under_the_bar(tmp_path):
    writes = [f"pkg/mod_{i}.py" for i in range(40)]
    footprints = {f"peer-{k}": frozenset(f"peer{k}/mod_{j}.py" for j in range(200)) for k in range(50)}
    footprints["hit"] = frozenset({"pkg/mod_3.py", "pkg", "pkg/mod_3.py/sub.py", "other/x.py"})
    footprints["unstaged"] = None
    assert live_run_writes.overlaps(writes, footprints) == {
        "hit": ["pkg", "pkg/mod_3.py", "pkg/mod_3.py/sub.py"]
    }
    ms = in_process_time_ms(lambda: live_run_writes.overlaps(writes, footprints))["process_time_ms"]
    assert ms < BAR_MS
