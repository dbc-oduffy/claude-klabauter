"""A spine row whose change_kind is outside the plan-tasks schema enum is refused at staging and emit."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.spine_read import (
    InvalidChangeKindError,
    change_kind_members,
    read_spine,
)

PLAN_REL = "docs/plans/2026-10-16-ck.md"


def _plan(kind: str | None) -> str:
    ck = f"  change_kind: {kind}\n" if kind is not None else ""
    return (
        "---\ntitle: fixture\n---\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: A1\n  title: row\n" + ck + "  surface: pkg/a.py\n  writes:\n    - pkg/a.py\n```\n"
    )


def _repo(tmp_path, kind):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(_plan(kind), encoding="utf-8", newline="\n")


def test_members_come_from_the_schema():
    members = change_kind_members()
    assert "code-edit" in members
    assert "schema-edit" not in members


@pytest.mark.parametrize("bad", ["schema-edit", "nonsense"])
def test_stage_refuses_non_member_naming_row_and_field(tmp_path, bad):
    _repo(tmp_path, bad)
    reply = _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tmp_path)
    assert "INVALID-CHANGE-KIND" in reply["error"]
    assert "A1" in reply["error"] and "change_kind" in reply["error"] and bad in reply["error"]


def test_stage_accepts_member(tmp_path):
    _repo(tmp_path, "code-edit")
    reply = _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tmp_path)
    assert "error" not in reply


def test_emit_script_refuses_non_member(tmp_path):
    _repo(tmp_path, "schema-edit")
    with pytest.raises(InvalidChangeKindError, match="A1"):
        emit.emit_script(tmp_path / PLAN_REL, repo_root=str(tmp_path))


def test_enum_edited_after_first_read_is_seen_by_a_warm_engine(tmp_path, monkeypatch):
    import json

    from coordinator_core.ops.dispatch_emit import spine_read

    schema = tmp_path / "plan-tasks.schema.json"
    schema.write_text(json.dumps({"properties": {"change_kind": {"enum": ["code-edit"]}}}), encoding="utf-8")
    monkeypatch.setattr(spine_read, "_PLAN_TASKS_SCHEMA", schema)
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo, "config-edit")
    with pytest.raises(InvalidChangeKindError, match="config-edit"):
        read_spine(repo / PLAN_REL)

    schema.write_text(
        json.dumps({"properties": {"change_kind": {"enum": ["code-edit", "config-edit"]}}}), encoding="utf-8"
    )
    assert "config-edit" in change_kind_members()
    read_spine(repo / PLAN_REL)


def test_read_spine_refuses_non_member(tmp_path):
    _repo(tmp_path, "schema-edit")
    with pytest.raises(InvalidChangeKindError, match="change_kind"):
        read_spine(tmp_path / PLAN_REL)
