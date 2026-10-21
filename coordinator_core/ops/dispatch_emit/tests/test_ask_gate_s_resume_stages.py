"""dispatch.ask_gate on an S verdict with resume_plan stages the plan itself, limited to the resume rows."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo, *args):
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, **no_console_creationflags()
    )


def _committed_s_plan(repo, rows):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=rows)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")


def test_s_resume_plan_verdict_carries_a_manifest_of_only_the_resume_rows(repo):  # noqa: F811
    _committed_s_plan(repo, [("A1", "coded"), ("A2", "open"), ("A3", "coded"), ("A4", "open")])

    out = _handler({"sizing_path": REL, "run_id": "r1", "writes": ["a.py"]}, repo_root=repo)

    assert out["halt"] is None and out["arm"] == "s" and out["resume_rows"] == ["A2", "A4"], out
    assert "manifest_handle" in out and "manifest" not in out, out
    manifest = read_reply(repo, out["manifest_handle"])
    assert manifest["run_dir"] and manifest["marker_path"] and "head_sha" in manifest
    assert sorted(r["id"] for r in manifest["rows"]) == ["A2", "A4"], manifest


def test_s_resume_plan_without_run_id_carries_no_manifest(repo):  # noqa: F811
    _committed_s_plan(repo, [("A1", "open")])

    out = _handler({"sizing_path": REL}, repo_root=repo)

    assert out["resume_plan"] == PLAN_REL and "manifest" not in out


def test_s_without_resume_plan_carries_no_manifest(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    (repo / ".git").mkdir(exist_ok=True)

    out = _handler({"sizing_path": REL, "run_id": "r1"}, repo_root=repo)

    assert "manifest" not in out
