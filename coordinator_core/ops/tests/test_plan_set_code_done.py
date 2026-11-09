"""
coordinator_core/ops/tests/test_plan_set_code_done.py — the "plan.set_code_done" op.

Subject: `coordinator_core.ops.plan_set_code_done`. Handler is called directly over
`tmp_path` fixtures (a bare tree with a `.git` DIRECTORY); zero spawns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import plan_set_code_done as mod


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    return tmp_path


def _row(row_id: str, disposition: str, agent_type: str | None = "coordinator:executor") -> str:
    lines = [
        f"- id: {row_id}",
        "  title: Ship it",
        "  body: Do the named work.",
        "  change_kind: code-edit",
        "  surface: a.py",
        "  writes: [a.py]",
        "  queue_scope: project",
        f"  disposition: {disposition}",
    ]
    if agent_type is not None:
        lines.append(f"  agent_type: {agent_type}")
    return "\n".join(lines)


def _plan(root: Path, name: str, rows: list) -> str:
    spine = "\n".join(rows)
    (root / "docs" / "plans" / name).write_text(
        "---\ntitle: fixture\nstatus: draft\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        + spine
        + "\n```\n",
        encoding="utf-8",
    )
    return f"docs/plans/{name}"


def _run(root: Path, plans: list) -> dict:
    return mod._handler({"plans": plans}, root)


def test_open_executor_row_holds_the_verdict_false_and_is_named(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root, "a.md", [_row("C1", "open"), _row("C2", "coded")])
    reply = _run(root, [plan])
    assert reply["code_complete"] is False
    assert reply["plans"] == [{"plan": plan, "open_rows": ["C1"], "error": None}]


def test_all_rows_closed_is_code_complete(tmp_path):
    root = _repo(tmp_path)
    a = _plan(root, "a.md", [_row("C1", "coded"), _row("C2", "done"), _row("C3", "voided")])
    b = _plan(root, "b.md", [_row("C1", "wont_do")])
    reply = _run(root, [a, b])
    assert reply["code_complete"] is True
    assert [p["open_rows"] for p in reply["plans"]] == [[], []]


def test_one_open_plan_in_a_set_holds_the_whole_set(tmp_path):
    root = _repo(tmp_path)
    a = _plan(root, "a.md", [_row("C1", "coded")])
    b = _plan(root, "b.md", [_row("C1", "open")])
    reply = _run(root, [a, b])
    assert reply["code_complete"] is False
    assert reply["plans"][0]["open_rows"] == []
    assert reply["plans"][1]["open_rows"] == ["C1"]


def test_open_non_executor_row_does_not_hold_the_verdict(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root, "a.md", [_row("C1", "coded"), _row("R1", "open", "coordinator:em")])
    assert _run(root, [plan])["code_complete"] is True


def test_open_row_without_agent_type_counts_as_executor(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root, "a.md", [_row("C1", "open", None)])
    reply = _run(root, [plan])
    assert reply["code_complete"] is False
    assert reply["plans"][0]["open_rows"] == ["C1"]


def test_unrecognized_disposition_counts_as_open(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root, "a.md", [_row("C1", "someday")])
    assert _run(root, [plan])["code_complete"] is False


def test_missing_plan_is_false_with_a_reason(tmp_path):
    root = _repo(tmp_path)
    reply = _run(root, ["docs/plans/nope.md"])
    assert reply["code_complete"] is False
    assert reply["plans"][0]["error"] == "no such plan"


def test_plan_without_a_spine_is_false_with_a_reason(tmp_path):
    root = _repo(tmp_path)
    (root / "docs" / "plans" / "bare.md").write_text("---\ntitle: x\n---\n\n# no spine\n", encoding="utf-8")
    reply = _run(root, ["docs/plans/bare.md"])
    assert reply["code_complete"] is False
    assert "not LOCATED" in reply["plans"][0]["error"]


def test_located_spine_with_zero_rows_is_false_with_a_reason(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root, "empty.md", ["[]"])
    reply = _run(root, [plan])
    assert reply["code_complete"] is False
    assert reply["plans"][0]["error"] == "task spine has no rows"


def test_path_escaping_the_worktree_is_false_with_a_reason(tmp_path):
    (tmp_path / "repo").mkdir()
    root = _repo(tmp_path / "repo")
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    reply = _run(root, ["../outside.md"])
    assert reply["code_complete"] is False
    assert "escapes" in reply["plans"][0]["error"]


def test_one_bad_plan_does_not_hide_the_others(tmp_path):
    root = _repo(tmp_path)
    good = _plan(root, "a.md", [_row("C1", "open")])
    reply = _run(root, ["docs/plans/nope.md", good])
    assert reply["plans"][0]["error"] == "no such plan"
    assert reply["plans"][1]["open_rows"] == ["C1"]


@pytest.mark.parametrize("plans", [None, [], "docs/plans/a.md"])
def test_plans_must_be_a_non_empty_list(tmp_path, plans):
    root = _repo(tmp_path)
    with pytest.raises(ValueError):
        mod._handler({"plans": plans}, root)


def test_requires_a_repo_root():
    with pytest.raises(ValueError):
        mod._handler({"plans": ["x.md"]}, None)


def test_the_op_writes_nothing(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root, "a.md", [_row("C1", "open")])
    before = sorted(p.as_posix() for p in root.rglob("*") if p.is_file())
    stat = (root / plan).stat().st_mtime_ns
    _run(root, [plan])
    assert sorted(p.as_posix() for p in root.rglob("*") if p.is_file()) == before
    assert (root / plan).stat().st_mtime_ns == stat


def test_malformed_agent_type_fails_closed_as_an_executor_row():
    assert mod._is_executor_row({"agent_type": ["coordinator:em"]}) is True
    assert mod._is_executor_row({"agent_type": "coordinator:em"}) is False

