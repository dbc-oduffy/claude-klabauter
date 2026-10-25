"""The residue workflow lands exactly the paths its stages wrote, and the rulings op declares its plans."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.roadmap.tests.test_mise_prep_residue_workflow import SOURCE
from coordinator_core.session.declared_writes import collecting

_PLAN = "---\nstatus: open\ntitle: t\n---\n\nbody\n"


def test_rulings_handler_declares_exactly_the_applied_plans(tmp_path, monkeypatch):
    from coordinator_core.ops import plan_record_residue_rulings as op

    (tmp_path / "docs" / "plans").mkdir(parents=True)
    ok = tmp_path / "docs" / "plans" / "ok.md"
    ok.write_text(_PLAN, encoding="utf-8")
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(op, "main_worktree_root", lambda r: str(tmp_path))
    rulings = [
        {"id": "census[0]", "item_kind": "census-leftover", "plan": "docs/plans/ok.md", "ruling": "drop", "pmOnly": False},
        {"id": "census[1]", "item_kind": "census-leftover", "plan": "docs/plans/missing.md", "ruling": "drop", "pmOnly": False},
        {"id": "census[2]", "item_kind": "census-leftover", "plan": "../escape.md", "ruling": "drop", "pmOnly": False},
    ]
    with collecting() as got:
        reply = op._handler({"run_id": "r", "on": "2026-10-23", "rulings": rulings}, repo_root=tmp_path)
    assert [a["plan"] for a in reply["applied"]] == ["docs/plans/ok.md"]
    assert len(reply["refused"]) == 2
    assert [Path(p).resolve() for p in got] == [ok.resolve()]


def test_source_lands_through_commit_v2_only():
    text = SOURCE.read_text(encoding="utf-8")
    assert "ceremony.commit_v2" in text
    assert '["commit"' not in text and "'commit'," not in text
    assert "landPaths.length > 0" in text


def test_land_leg_runs_as_the_commit_agent_the_guard_admits():
    text = SOURCE.read_text(encoding="utf-8")
    land = text[text.index("label: 'land-residue'"):]
    land = land[:land.index("schema:")]
    assert "agentType: 'coordinator:git-commit-agent'" in land
    assert "residue.agentType" not in land
