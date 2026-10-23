"""`dispatch.ask_stage` parses a full plan once per handler call and keeps stage()'s error order."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.ask_stage import _handler

PLAN_REL = "docs/plans/2026-10-23-parse-once.md"


def _plan(kind: str = "code-edit", extra: str = "") -> str:
    return (
        "---\ntitle: fixture\n---\n# Fixture\n" + extra + "\n## Tasks\n\n```yaml plan-tasks\n"
        f"- id: A1\n  title: row\n  change_kind: {kind}\n  surface: pkg/a.py\n  writes:\n    - pkg/a.py\n```\n"
    )


def _repo(tmp_path, text):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(text, encoding="utf-8", newline="\n")


def _count(monkeypatch):
    counts = {"load_rows": 0, "read_spine": 0}
    real_load, real_spine = ask_stage.load_rows, ask_stage.read_spine

    def load_rows(*a, **k):
        counts["load_rows"] += 1
        return real_load(*a, **k)

    def read_spine(*a, **k):
        counts["read_spine"] += 1
        return real_spine(*a, **k)

    monkeypatch.setattr(ask_stage, "load_rows", load_rows)
    monkeypatch.setattr(ask_stage, "read_spine", read_spine)
    return counts


def test_a_full_plan_handler_call_parses_once(tmp_path, monkeypatch):
    _repo(tmp_path, _plan())
    counts = _count(monkeypatch)
    reply = _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tmp_path)
    assert "error" not in reply, reply
    assert counts == {"load_rows": 1, "read_spine": 1}


def test_claim_register_rows_is_not_preparsed(tmp_path, monkeypatch):
    _repo(tmp_path, _plan())
    counts = _count(monkeypatch)
    reply = _handler({"run_id": "r1", "plan_path": PLAN_REL, "claim_register_rows": True}, repo_root=tmp_path)
    assert "error" not in reply, reply
    assert counts == {"load_rows": 2, "read_spine": 2}


def test_a_scaffold_marked_plan_still_raises_the_scaffold_error(tmp_path):
    _repo(tmp_path, _plan(extra="\nPLACEHOLDER\n"))
    reply = _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tmp_path)
    assert "PLAN-SCAFFOLD-UNFILLED" in reply["error"]


def test_a_bad_change_kind_still_raises_the_change_kind_error(tmp_path):
    _repo(tmp_path, _plan(kind="nonsense"))
    reply = _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tmp_path)
    assert "INVALID-CHANGE-KIND" in reply["error"]


def test_an_escaping_row_is_still_refused_on_the_shared_parse(tmp_path):
    text = _plan().replace("pkg/a.py", "../sibling/x.md")
    _repo(tmp_path, text)
    reply = _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tmp_path)
    assert "../sibling/x.md" in reply["error"]
