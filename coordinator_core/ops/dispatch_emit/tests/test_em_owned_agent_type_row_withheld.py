"""A row declaring an EM-owned agent_type is withheld as em-performed and spawns no agent."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.em_only_act import row_is_em_performed
from coordinator_core.ops.dispatch_emit.spine_read import read_spine


def _plan(tmp_path, agent_type):
    (tmp_path / ".git").mkdir(exist_ok=True)
    row = (
        "- id: A1\n  title: agent row\n  change_kind: code-edit\n  surface: a.py\n"
        "  writes: [a.py]\n  disposition: open\n  body: write a\n"
        "- id: M1\n  title: em merge\n  change_kind: code-edit\n  surface: b.py\n"
        f"  writes: [b.py]\n  agent_type: {agent_type}\n  disposition: open\n  body: merge b\n"
    )
    path = tmp_path / "p.md"
    path.write_text(
        "---\ntitle: p\n---\n\n# p\n\n## Goal\n\ng\n\n## Tasks\n\n```yaml plan-tasks\n" + row + "```\n",
        encoding="utf-8",
    )
    return path


def _run(tmp_path, agent_type):
    out: list = []
    rows = read_spine(_plan(tmp_path, agent_type), exclusions=out)
    return rows, out


def test_em_owned_row_is_withheld_and_spawns_no_agent(tmp_path):
    rows, out = _run(tmp_path, "coordinator:em")
    assert [(e["id"], e["reason"]) for e in out] == [("M1", "em-performed")]
    assert [r.id for r in rows] == ["A1"]


def test_agent_type_executor_row_is_still_dispatched(tmp_path):
    rows, out = _run(tmp_path, "coordinator:executor")
    assert out == []
    assert sorted(r.id for r in rows) == ["A1", "M1"]


def test_predicate_pins_both_sides():
    assert row_is_em_performed({"agent_type": "coordinator:em"}) is True
    assert row_is_em_performed({"agent_type": "coordinator:executor"}) is False
