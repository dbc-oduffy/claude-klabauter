"""ask_gate halts `nothing-executable` before arming when the bound plan has no dispatchable row."""

from __future__ import annotations

import time

import pytest

from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL

pytestmark = pytest.mark.complete

_GATED = "  external_gate:\n    - owner_repo: other-repo\n      requires: landed-work\n"
_SPEC = "  change_kind: code-edit\n  surface: pkg/{i}.py\n  writes:\n    - pkg/{i}.py\n"


def _spine(repo, rows) -> None:
    """`rows` is `[(id, disposition, extra yaml), ...]`."""
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: {d}\n" + _SPEC.format(i=i) + extra for i, d, extra in rows
    )
    path = repo / PLAN_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n")


def test_gated_only_plan_halts_nothing_executable_naming_each_row(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _spine(repo, [("G1", "open", _GATED), ("C1", "coded", "")])
    v = gate(repo, REL)
    assert v.arm is None and v.halt["kind"] == "nothing-executable", v.halt
    assert [r["id"] for r in v.halt["rows"]] == ["G1"]
    assert "G1" in v.halt["reason"] and v.halt["rows"][0]["reason"]

def test_operator_only_plan_halts_nothing_executable(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _spine(repo, [("E1", "open", "  execution_mode: operator\n")])
    v = gate(repo, REL)
    assert v.arm is None and v.halt["kind"] == "nothing-executable", v.halt
    assert [r["id"] for r in v.halt["rows"]] == ["E1"]


def test_all_coded_plan_keeps_the_all_landed_path(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _spine(repo, [("C1", "coded", ""), ("C2", "coded", "")])
    v = gate(repo, REL)
    assert (v.halt or {}).get("kind") != "nothing-executable", v.halt


def test_one_open_dispatchable_row_arms_unchanged(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _spine(repo, [("G1", "open", _GATED), ("A1", "open", "")])
    v = gate(repo, REL)
    assert v.halt is None and v.arm == "s" and "A1" in v.resume_rows, v


def test_sizing_without_a_bound_plan_arms_unchanged(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    v = gate(repo, REL)
    assert v.halt is None and v.arm == "s" and v.resume_plan is None


def test_unreadable_plan_defers_to_the_stage(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    path = repo / PLAN_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("no spine here\n", encoding="utf-8")
    v = gate(repo, REL)
    assert (v.halt or {}).get("kind") != "nothing-executable"


def test_the_check_stays_under_500ms_process_time(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _spine(repo, [("G1", "open", _GATED)])
    gate(repo, REL)
    start = time.process_time()
    gate(repo, REL)
    assert time.process_time() - start < 0.5
