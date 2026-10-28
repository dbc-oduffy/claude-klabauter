"""Falsifier tests for the projects pruner (disk_budget.prune_projects).

Fixture trees live under tmp_path only; sizes are sparse files.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import disk_budget  # noqa: E402
from disk_budget import Policy, Roots, resolve_roots  # noqa: E402

MiB = 1024 * 1024
HOUR = 3600.0
NOW = 1_800_000_000.0
CAP = 12 * MiB


def _file(path: Path, size: int, age_hours: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)
    t = NOW - age_hours * HOUR
    os.utime(path, (t, t))
    return path


def _run_file(path: Path, status, age_hours: float, run_id: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"run_id": run_id}
    if status is not None:
        body["status"] = status
    path.write_text(json.dumps(body))
    with open(path, "ab") as fh:
        fh.truncate(2 * MiB)
    t = NOW - age_hours * HOUR
    os.utime(path, (t, t))
    return path


@pytest.fixture
def tree(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    proj = home / ".claude" / "projects" / "-Users-x-proj"
    live, old, dead = "sess-live", "sess-old", "sess-dead"

    spared = {
        "live_main": _file(proj / f"{live}.jsonl", 3 * MiB, 1),
        "memory": _file(proj / "memory" / "MEMORY.md", 1 * MiB, 500),
        "pm_turns": _file(proj / live / "pm_turns.jsonl", 1 * MiB, 500),
        "running_run": _run_file(proj / live / "workflows" / "wf_running.json", "running", 1, "run-live"),
        "unreadable_status_run": _run_file(proj / live / "workflows" / "wf_nostatus.json", None, 1, "run-x"),
        "fresh_subagent": _file(proj / live / "subagents" / "agent-fresh.jsonl", 2 * MiB, 2),
    }
    spared["fresh_subagent_meta"] = _file(proj / live / "subagents" / "agent-fresh.meta.json", 1, 2)

    resolved_run = _run_file(proj / old / "workflows" / "wf_done.json", "completed", 0.5, "run-done")
    script = _file(repo / "scratch" / "warp" / "run-done.workflow.mjs", 1000, 0.5)
    subagent = _file(proj / old / "subagents" / "agent-aged.jsonl", 4 * MiB, 30)
    subagent_meta = _file(proj / old / "subagents" / "agent-aged.meta.json", 1, 30)
    aged_main = _file(proj / f"{dead}.jsonl", 2 * MiB, 8 * 24)
    tool_result = _file(proj / old / "tool-results" / "t1.txt", 6 * MiB, 48)
    gone = {
        "resolved_run": resolved_run,
        "script": script,
        "aged_subagent": subagent,
        "aged_subagent_meta": subagent_meta,
        "aged_main": aged_main,
        "tool_result": tool_result,
    }
    roots = resolve_roots(home=home, repos=[repo])
    return roots, spared, gone


def _total(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _prune(roots: Roots, **kw):
    policy = kw.pop("policy", Policy(projects_cap=CAP, per_session_cap=CAP))
    return disk_budget.prune_projects(
        policy,
        time.monotonic() + 30,
        roots=roots,
        now=NOW,
        live_sessions=kw.pop("live_sessions", ("sess-live",)),
        **kw,
    )


def test_over_cap_tree_prunes_under_cap(tree):
    roots, _spared, _gone = tree
    assert _total(roots.projects) > CAP
    report = _prune(roots)
    assert _total(roots.projects) < CAP
    assert report.freed_bytes > 0
    assert report.still_over is False


def test_spare_list_survives(tree):
    roots, spared, _gone = tree
    _prune(roots)
    for name, path in spared.items():
        assert path.exists(), name


def test_resolved_run_script_and_aged_files_deleted_even_under_cap(tree):
    roots, _spared, gone = tree
    _prune(roots, policy=Policy(projects_cap=10 * 1024 * MiB, per_session_cap=10 * 1024 * MiB))
    for name in ("resolved_run", "script", "aged_subagent", "aged_subagent_meta", "aged_main"):
        assert not gone[name].exists(), name


def test_subagent_and_meta_are_one_unit(tree):
    roots, _spared, gone = tree
    _prune(roots)
    assert gone["aged_subagent"].exists() == gone["aged_subagent_meta"].exists()


def test_live_main_transcript_survives_even_when_aged(tree):
    roots, spared, _gone = tree
    t = NOW - 30 * 24 * HOUR
    os.utime(spared["live_main"], (t, t))
    _prune(roots)
    assert spared["live_main"].exists()


def test_non_live_young_main_transcript_survives(tree):
    roots, _spared, _gone = tree
    young = _file(roots.projects / "-Users-x-proj" / "sess-young.jsonl", MiB, 3 * 24)
    _prune(roots)
    assert young.exists()


def test_still_over_when_only_spared_bytes_remain(tree):
    roots, spared, _gone = tree
    report = _prune(roots, policy=Policy(projects_cap=MiB, per_session_cap=MiB))
    assert report.still_over is True
    assert spared["live_main"].exists()
    assert spared["memory"].exists()


def test_dry_run_deletes_nothing(tree):
    roots, spared, gone = tree
    report = _prune(roots, apply=False)
    assert all(p.exists() for p in gone.values())
    assert report.freed_bytes > 0


def test_per_session_ceiling_prunes_oldest_in_session(tmp_path):
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "-p"
    older = _file(proj / "s" / "subagents" / "agent-a.jsonl", 3 * MiB, 20)
    newer = _file(proj / "s" / "subagents" / "agent-b.jsonl", 3 * MiB, 12)
    roots = resolve_roots(home=home)
    policy = Policy(projects_cap=100 * MiB, per_session_cap=4 * MiB, subagent_max_age_hours=100, min_age_hours=0)
    _prune(roots, policy=policy, live_sessions=())
    assert not older.exists()
    assert newer.exists()


def _nested_tree(tmp_path):
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "-p"
    sess = proj / "sess-n"
    wf = sess / "subagents" / "workflows"
    keep = {
        "running_by_id": _file(wf / "wf_runid" / "agent-1.jsonl", MiB, 30),
        "running_by_state": _file(wf / "wf_young" / "agent-2.jsonl", MiB, 2),
        "fresh_top": _file(sess / "subagents" / "agent-new.jsonl", MiB, 2),
    }
    _run_file(sess / "workflows" / "wf_runid.json", "running", 30, "runid")
    _run_file(wf / "wf_young" / "state.json", "running", 2, "young")
    gone = {
        "aged_nested": _file(wf / "wf_old" / "agent-3.jsonl", MiB, 30),
        "aged_nested_meta": _file(wf / "wf_old" / "agent-3.meta.json", 1, 30),
        "aged_deep": _file(sess / "subagents" / "other" / "deep" / "agent-4.jsonl", MiB, 30),
        "terminal_nested": _file(wf / "wf_done" / "agent-5.jsonl", MiB, 3),
    }
    _run_file(wf / "wf_done" / "state.json", "completed", 3, "done")
    return resolve_roots(home=home), keep, gone


def test_nested_workflow_subagents_pruned_and_running_protected(tmp_path):
    roots, keep, gone = _nested_tree(tmp_path)
    policy = Policy(projects_cap=1024 * MiB, per_session_cap=1024 * MiB, subagent_max_age_hours=24)
    _prune(roots, policy=policy, live_sessions=())
    for name, path in keep.items():
        assert path.exists(), name
    for name in ("aged_nested", "aged_nested_meta", "aged_deep"):
        assert not gone[name].exists(), name
    assert gone["terminal_nested"].exists()


def test_nested_dry_run_counts_but_deletes_nothing(tmp_path):
    roots, keep, gone = _nested_tree(tmp_path)
    policy = Policy(projects_cap=1024 * MiB, per_session_cap=1024 * MiB, subagent_max_age_hours=24)
    report = _prune(roots, policy=policy, live_sessions=(), apply=False)
    assert report.freed_bytes >= 2 * MiB
    assert all(p.exists() for p in {**keep, **gone}.values())


def test_deadline_in_the_past_does_nothing_destructive(tree):
    roots, _spared, gone = tree
    disk_budget.prune_projects(
        Policy(projects_cap=CAP), time.monotonic() - 1, roots=roots, now=NOW, live_sessions=("sess-live",)
    )
    assert all(p.exists() for p in gone.values())
