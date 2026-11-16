"""
coordinator_core.ops.tests.test_plan_tasks_mutate_backlogged_repo

plan.tasks.mutate resolve/backlogged runs the REAL coordinator-harvest-
deferrals module in-process (only its child spawn is faked), with the module's
process-wide repo memo pointing at repo A while the plan lives in repo B.
Pins: the queue row lands in B only, the child runs with cwd B, and a
caller-supplied repo-relative disposition_ref is kept (an absolute one is not).
"""

from __future__ import annotations

import re
import subprocess
import types
from pathlib import Path

import pytest

import coordinator_core.ops.plan_tasks_mutate as plan_tasks_mutate
from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.ops.tests.test_plan_tasks_mutate import (
    _PLAN_WITH_TASKS,
    _make_git_repo,
    _run,
    _seed_plan,
)

_PLAN_ID = "pln-backlogged-repo-b-test"


def _two_repos(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    return _make_git_repo(tmp_path / "a"), _make_git_repo(tmp_path / "b")


@pytest.fixture
def harvest_in_repo_a(tmp_path, monkeypatch):
    """Real harvest module, memo pinned to repo A, child spawn recorded and
    emulated by writing the queue entry under the spawn's cwd."""
    repo_a, repo_b = _two_repos(tmp_path)
    monkeypatch.setattr(plan_tasks_mutate, "_STAMP_REFUSED_APPROVAL_FIELDS", frozenset())
    monkeypatch.delenv("QUEUE_APPEND_OUTPUT_ROOT", raising=False)
    monkeypatch.delenv("LESSON_PROMOTE_OUTBOX_ROOT", raising=False)

    harvest = plan_tasks_mutate._load_harvest_module()
    monkeypatch.setitem(harvest._repo_root_cache, harvest._UNSET, str(repo_a))
    monkeypatch.setattr(harvest, "_resolved_claude_klabauter_root", lambda: None)
    monkeypatch.setattr(harvest, "_resolved_content_root", lambda: None)
    monkeypatch.setattr(harvest, "_resolve_cli_cmd", lambda name: [name])
    monkeypatch.setattr(harvest, "_child_identity_env", lambda: {})

    spawns: list[str | None] = []

    def _fake_run(cmd, **kwargs):
        cwd = kwargs.get("cwd") or str(repo_a)
        spawns.append(kwargs.get("cwd"))
        key = cmd[cmd.index("--evidence") + 1]
        queue = Path(cwd) / "state" / "improvement-queue"
        queue.mkdir(parents=True, exist_ok=True)
        (queue / "entry-C1.yaml").write_text(f"evidence: {key}\n", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        harvest,
        "subprocess",
        types.SimpleNamespace(run=_fake_run, TimeoutExpired=subprocess.TimeoutExpired),
    )
    return types.SimpleNamespace(repo_a=repo_a, repo_b=repo_b, spawns=spawns)


def _resolve_backlogged(repo: Path, plan: Path, **extra) -> dict:
    handler_root = repo / ".git"
    _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=handler_root,
    ))
    return _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "deferred to backlog",
            "case_against": "waiting costs little; nothing depends on this landing now",
            **extra,
        },
        repo_root=handler_root,
    ))


def _plan_in(repo: Path) -> Path:
    text = _PLAN_WITH_TASKS.replace("status: draft", f'status: draft\nplan_id: "{_PLAN_ID}"', 1)
    return _seed_plan(repo, "backlogged-repo.md", text)


def _disposition_ref(plan: Path) -> str:
    m = re.search(r"disposition_ref:\s*(\S+)\s*$", plan.read_text(encoding="utf-8"), re.MULTILINE)
    assert m, plan.read_text(encoding="utf-8")
    return m.group(1).strip('"')


def test_backlogged_row_lands_in_plan_repo_not_memoized_repo(harvest_in_repo_a):
    ctx = harvest_in_repo_a
    plan = _plan_in(ctx.repo_b)

    result = _resolve_backlogged(ctx.repo_b, plan)

    assert result["exit_code"] == 0, result
    assert (ctx.repo_b / "state" / "improvement-queue" / "entry-C1.yaml").is_file()
    assert not (ctx.repo_a / "state").exists()
    assert ctx.spawns == [str(ctx.repo_b)]
    assert _disposition_ref(plan) == "state/improvement-queue/entry-C1.yaml"


def test_scope_does_not_leak_into_standalone_repo_resolution(harvest_in_repo_a):
    ctx = harvest_in_repo_a
    harvest = plan_tasks_mutate._load_harvest_module()

    _resolve_backlogged(ctx.repo_b, _plan_in(ctx.repo_b))

    assert harvest._repo_root() == str(ctx.repo_a)


def test_caller_repo_relative_disposition_ref_is_kept(harvest_in_repo_a):
    ctx = harvest_in_repo_a
    plan = _plan_in(ctx.repo_b)
    caller = ctx.repo_b / "state" / "improvement-queue" / "caller-chosen.yaml"
    caller.parent.mkdir(parents=True)
    caller.write_text("evidence: x\n", encoding="utf-8")

    result = _resolve_backlogged(
        ctx.repo_b, plan, disposition_ref="state/improvement-queue/caller-chosen.yaml"
    )

    assert result["exit_code"] == 0, result
    assert _disposition_ref(plan) == "state/improvement-queue/caller-chosen.yaml"
    assert (ctx.repo_b / "state" / "improvement-queue" / "entry-C1.yaml").is_file()


def test_caller_absolute_disposition_ref_is_replaced_by_computed_ref(harvest_in_repo_a):
    ctx = harvest_in_repo_a
    plan = _plan_in(ctx.repo_b)

    result = _resolve_backlogged(
        ctx.repo_b, plan, disposition_ref=str(ctx.repo_a / "state" / "elsewhere.yaml")
    )

    assert result["exit_code"] == 0, result
    assert _disposition_ref(plan) == "state/improvement-queue/entry-C1.yaml"


def test_caller_repo_relative_ref_to_missing_file_is_replaced(harvest_in_repo_a):
    ctx = harvest_in_repo_a
    plan = _plan_in(ctx.repo_b)

    result = _resolve_backlogged(
        ctx.repo_b, plan, disposition_ref="state/improvement-queue/never-written.yaml"
    )

    assert result["exit_code"] == 0, result
    assert _disposition_ref(plan) == "state/improvement-queue/entry-C1.yaml"
