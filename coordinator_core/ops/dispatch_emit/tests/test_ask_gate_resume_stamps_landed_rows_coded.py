"""The gate stamps a row resume's checkpoint-landed rows `coded` with their landing sha."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_resume_counts_ledger_halt_landed_chunks import (
    REGISTER,
    _checkpoint,
    _git,
    _halt_commit,
    _load,
    _pointer,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import _plan

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

ROWS = [("C1", "open"), ("C2", "open"), ("C3", "open")]


def _init(repo, rows=ROWS):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _plan(repo, rows=rows)
    _put(repo, tshirt="S", route="spec-dispatch", plan=PLAN_REL, requirement_register=REGISTER)


def _head(repo) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _rows(repo) -> dict:
    import yaml

    text = (repo / PLAN_REL).read_text(encoding="utf-8")
    block = text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0]
    return {r["id"]: r for r in yaml.safe_load(block)}


def test_gate_stamps_checkpoint_landed_row_coded_with_its_sha(repo):  # noqa: F811
    _init(repo)
    _checkpoint(repo, ["C1"])
    sha = _head(repo)
    reply = _handler({"sizing_path": REL}, repo)
    assert reply["resume_rows"] == ["C2", "C3"]
    rows = _rows(repo)
    assert rows["C1"]["disposition"] == "coded" and rows["C1"]["disposition_ref"] == sha
    assert rows["C2"]["disposition"] == "open" and "disposition_ref" not in rows["C2"]
    assert rows["C3"]["disposition"] == "open"


def test_unlanded_plan_is_byte_identical_after_the_gate(repo):  # noqa: F811
    _init(repo)
    before = (repo / PLAN_REL).read_bytes()
    reply = _handler({"sizing_path": REL}, repo)
    assert reply["resume_rows"] == ["C1", "C2", "C3"]
    assert (repo / PLAN_REL).read_bytes() == before


def test_non_open_rows_are_left_alone(repo):  # noqa: F811
    _init(repo, rows=[("C1", "open"), ("C2", "delivered_elsewhere"), ("C3", "open")])
    _checkpoint(repo, ["C1", "C2"])
    _handler({"sizing_path": REL}, repo)
    rows = _rows(repo)
    assert rows["C1"]["disposition"] == "coded"
    assert rows["C2"]["disposition"] == "delivered_elsewhere" and "disposition_ref" not in rows["C2"]


def test_stamp_that_breaks_the_spine_source_check_is_not_written(repo, monkeypatch):  # noqa: F811
    from coordinator_core.ops.dispatch_emit import ask_gate

    _init(repo)
    _checkpoint(repo, ["C1"])
    before = (repo / PLAN_REL).read_bytes()
    calls = []

    def check(text):
        calls.append(text)
        return None if len(calls) == 2 else "broken"

    monkeypatch.setattr("coordinator_core.frontmatter.schema_validate.check_plan_tasks_source", check)
    reply = _handler({"sizing_path": REL}, repo)
    assert reply["resume_rows"] == ["C2", "C3"]
    assert (repo / PLAN_REL).read_bytes() == before
    assert ask_gate is not None


def test_second_ledger_halt_after_a_stamp_still_resumes_at_the_ledger(repo):  # noqa: F811
    _init(repo, rows=[("C1", "open"), ("C2", "open")])
    _checkpoint(repo, ["C1"])
    _handler({"sizing_path": REL}, repo)
    assert _rows(repo)["C1"]["disposition"] == "coded"
    _halt_commit(repo, ["C2"])
    _pointer(repo)
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan.incomplete == () and plan.ledger is not None
