"""Brightline perf/spawn pins for ``emit_script`` on the DAG shape
(§ Design D4). Pins AC16.

Spec: docs/plans/2026-09-27-emitter-dag-terminal-commit-wake-digest.md,
row C12.
"""

from __future__ import annotations

import subprocess
import time

from coordinator_core.ops.dispatch_emit import emit as emit_mod
from coordinator_core.ops.dispatch_emit import pathspec as pathspec_mod
from coordinator_core.ops.dispatch_emit.emit import emit_script


def _write_forty_row_plan(tmp_path):
    lines = [
        "---\n---\n\n# Forty-row plan\n\n## Tasks\n\n",
        "```yaml plan-tasks\n",
    ]
    for i in range(1, 41):
        lines.append(f"- id: C{i}\n")
        lines.append(f"  title: Row {i}\n")
        lines.append("  change_kind: script-edit\n")
        lines.append(f"  surface: pkg/row{i}.py\n")
        lines.append("  writes:\n")
        lines.append(f"    - pkg/row{i}.py\n")
    lines.append("```\n")
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("".join(lines), encoding="utf-8")
    return plan_path


def test_emit_script_forty_row_fixture_is_fast_and_spawns_at_most_once(tmp_path, monkeypatch):
    plan_path = _write_forty_row_plan(tmp_path)

    spawn_count = 0
    real_run = subprocess.run

    def _counting_run(*args, **kwargs):
        nonlocal spawn_count
        spawn_count += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)

    start = time.process_time()
    emit_script(str(plan_path), repo_root=tmp_path)
    elapsed = time.process_time() - start

    assert elapsed < 0.2
    assert spawn_count <= 1


def test_map_written_path_to_test_target_called_at_most_once_per_distinct_path(tmp_path, monkeypatch):
    plan_path = _write_forty_row_plan(tmp_path)

    calls = []
    real_map = pathspec_mod._map_written_path_to_test_target

    def _counting_map(path, **kwargs):
        calls.append(path)
        return real_map(path, **kwargs)

    monkeypatch.setattr(emit_mod, "_map_written_path_to_test_target", _counting_map)

    emit_script(str(plan_path), repo_root=tmp_path)

    assert len(calls) <= 40
    assert len(calls) == len(set(calls))
