"""A transitively withheld row names its root: gate roots lend their owner, owed roots make it owed."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_stage import _handler

PLAN_REL = "docs/plans/2026-10-15-withheld-owner.md"


def _row(row_id: str, extra: str = "", dep: str | None = None) -> str:
    deps = f"  depends_on:\n    - chunk: {dep}\n      gate_kind: output-consumption-runtime\n" if dep else ""
    return (
        f"- id: {row_id}\n  title: row {row_id}\n  change_kind: code-edit\n"
        f"  surface: pkg/{row_id}.py\n  writes:\n    - pkg/{row_id}.py\n{extra}{deps}"
    )


def _stage(tmp_path, *rows: str):
    plan = "---\ntitle: fixture\n---\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n" + "".join(rows) + "```\n"
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (tmp_path / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    reply = _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tmp_path)
    assert "error" not in reply
    return reply


_GATE = (
    "  external_gate:\n    - owner_repo: other\n      requires: landed-work\n"
    "      closure_key: K\n"
)


def test_transitive_row_inherits_root_gate_owner(tmp_path):
    reply = _stage(tmp_path, _row("U1"), _row("G1", _GATE), _row("T1", dep="G1"))
    by_id = {g["id"]: g for g in reply["gated"]}
    assert by_id["T1"]["owner_repo"] == "other"
    assert by_id["T1"]["closure_key"] == "K"


def test_em_rooted_dependent_is_owed_not_gated(tmp_path):
    reply = _stage(tmp_path, _row("U1"), _row("E1", "  performer: em\n"), _row("T1", dep="E1"))
    assert ("T1", "withheld_by_owed_dependency") in [(o["id"], o["reason"]) for o in reply["owed"]]
    assert "T1" not in [g["id"] for g in reply["gated"]]


def test_no_gated_entry_has_empty_owner(tmp_path):
    pred = tmp_path / "docs" / "plans" / "pred.md"
    pred.parent.mkdir(parents=True, exist_ok=True)
    pred.write_text(
        "# p\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: P1\n  title: pred\n  surface: a.py\n  writes: [a.py]\n```\n",
        encoding="utf-8",
    )
    reply = _stage(
        tmp_path,
        _row("U1"),
        _row("G1", _GATE),
        _row("TG", dep="G1"),
        _row("E1", "  performer: em\n"),
        _row("TE", dep="E1"),
        _row("O1", "  execution_mode: operator\n"),
        _row("TO", dep="O1"),
        _row("H1", "  depends_on_plan:\n    - plan: docs/plans/pred.md\n      chunk: P1\n"),
        _row("TH", dep="H1"),
    )
    assert reply["gated"]
    assert all(g["owner_repo"] for g in reply["gated"])
    assert "TH" not in [g["id"] for g in reply["gated"]]


def test_judge_engine_clause_excludes_withheld_reasons(monkeypatch, tmp_path):
    import coordinator_core.ops.dispatch_emit.engine_owned_rows as eor
    from coordinator_core.ops.dispatch_emit.emit import _judge_engine_clause

    seen: list[str] = []

    def _capture(raw_rows, *_a, **_k):
        seen.extend(r["id"] for r in raw_rows)
        return []

    monkeypatch.setattr(eor, "engine_owner_rows", _capture)
    plan = "## Tasks\n\n```yaml plan-tasks\n" + _row("A") + _row("B") + _row("C") + _row("D") + "```\n"
    excluded = [
        {"id": "B", "reason": "withheld_by_owed_dependency"},
        {"id": "C", "reason": "withheld_by_held_dependency"},
        {"id": "D", "reason": "external_gate"},
    ]
    _judge_engine_clause("p.md", plan, tmp_path, None, excluded)
    assert seen == ["A"]
