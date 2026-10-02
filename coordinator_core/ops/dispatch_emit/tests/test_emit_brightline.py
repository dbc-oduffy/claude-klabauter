"""Brightline perf/spawn pins for ``emit_script`` on the DAG shape
(§ Design D4). Pins AC16.

Spec: docs/plans/2026-09-27-emitter-dag-terminal-commit-wake-digest.md,
row C12.
"""

from __future__ import annotations
from .conftest import REVIEW_KW

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
    emit_script(str(plan_path), repo_root=tmp_path, **REVIEW_KW)
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

    emit_script(str(plan_path), repo_root=tmp_path, **REVIEW_KW)

    assert len(calls) <= 40
    assert len(calls) == len(set(calls))


def _write_two_row_plan(tmp_path, name, surface):
    plan_path = tmp_path / name
    plan_path.write_text(
        "---\n---\n\n# Two-row plan\n\n## Tasks\n\n"
        "```yaml plan-tasks\n"
        "- id: C1\n  title: Row 1\n  change_kind: script-edit\n"
        "  surface: pkg/row1.py\n  writes:\n    - pkg/row1.py\n"
        "- id: C2\n  title: Row 2\n  change_kind: script-edit\n"
        f"  surface: {surface}\n  writes:\n    - {surface}\n"
        "```\n",
        encoding="utf-8",
    )
    return plan_path


def test_memo_shaped_row_costs_the_same_spawns_and_process_time(tmp_path, monkeypatch):
    receiver = tmp_path / "receiver-repo"
    (receiver / ".git").mkdir(parents=True)
    machine_local = tmp_path / "claude-home" / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    (machine_local / "registry.local.toml").write_text(
        f'"repos.receiver_repo" = "{receiver.as_posix()}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))

    outbox = tmp_path / ".coordinator-local" / "memo-outbox"
    outbox.mkdir(parents=True)
    memo_surface = ".coordinator-local/memo-outbox/topic.md"
    (tmp_path / memo_surface).write_text(
        '---\ntitle: "A test memo"\nfrom: "sender-em"\nto: "receiver-repo-em"\n'
        "status: draft\n---\n\nBody.\n",
        encoding="utf-8",
    )
    memo_plan = _write_two_row_plan(tmp_path, "memo-plan.md", memo_surface)
    plain_plan = _write_two_row_plan(tmp_path, "plain-plan.md", "pkg/other.py")

    real_run = subprocess.run
    counts = []

    def _counting_run(*args, **kwargs):
        counts[-1] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)

    times = []
    for plan_path in (plain_plan, memo_plan):
        counts.append(0)
        start = time.process_time()
        emit_script(str(plan_path), repo_root=tmp_path, **REVIEW_KW)
        times.append(time.process_time() - start)

    assert counts[0] == counts[1]
    assert abs(times[1] - times[0]) < 0.2
