"""roadmap.link_plan: governing_plan lands on a roadmap baton and plan_gate resolves it."""

from __future__ import annotations

import pytest

from coordinator_core.ops import roadmap_link_plan as op
from coordinator_core.roadmap import gate_v2

PLAN = "docs/plans/2026-10-21-derive.md"


def _setup(root, **baton_extra):
    (root / ".git").mkdir()
    (root / "docs/plans").mkdir(parents=True)
    (root / PLAN).write_text(
        "---\ntitle: derive\nplan_id: pln-derive\nstatus: implemented\ndeliverable_id: dlv-derive\n---\nbody\n",
        encoding="utf-8",
    )
    (root / "docs/plans/2026-10-21-derive.review.md").write_text(
        "---\ntitle: r\nkind: staff-eng-review\nstatus: done\n---\n", encoding="utf-8"
    )
    (root / "docs/plans/not-a-plan.md").write_text("no frontmatter\n", encoding="utf-8")
    h = root / "state/handoffs"
    h.mkdir(parents=True)
    lines = ["stub_id: eet-22", "kind: roadmap-baton", "roadmap_id: eet", "baton_role: work",
             "status: open", "deployment_state: ready_to_fire"]
    lines += [f"{k}: {v}" for k, v in baton_extra.items()]
    (h / "2026-10-21_eet-22.md").write_text("---\n" + "\n".join(lines) + "\n---\nbody\n", encoding="utf-8")
    (h / "other.md").write_text("---\nstub_id: other\nkind: handoff\nstatus: open\n---\n", encoding="utf-8")


def _call(root, **kw):
    p = {"stub_id": "eet-22", "plan": PLAN, "reason": "work landed via warp ask"}
    p.update(kw)
    return op._handler(p, repo_root=root / ".git")


def _disp(root):
    return next(b for b in gate_v2.compute_gate_report(root)["batons"] if b["id"] == "eet-22")


def test_eet22_shape_goes_from_unplanned_to_the_plans_disposition(tmp_path, monkeypatch):
    monkeypatch.setattr(op, "main_worktree_root", lambda r: tmp_path)
    _setup(tmp_path)
    assert _disp(tmp_path)["plan"] is None
    out = _call(tmp_path)
    assert out["exit_code"] == 0 and out["applied"] is True
    row = _disp(tmp_path)
    assert row["plan"]["path"] == PLAN and row["plan"]["status"] == "implemented"
    assert row["plan"]["link_basis"] == "governing_plan"
    again = _call(tmp_path)
    assert again["exit_code"] == 0 and again["applied"] is False


@pytest.mark.parametrize("kw,needle", [
    ({"plan": "docs/plans/missing.md"}, "existing .md"),
    ({"plan": "docs/plans/not-a-plan.md"}, "not a plan record"),
    ({"plan": "docs/plans/2026-10-21-derive.review.md"}, "not a plan record"),
    ({"plan": "../outside.md"}, "existing .md"),
    ({"stub_id": "other"}, "not a roadmap baton"),
    ({"stub_id": "ghost"}, "no live baton"),
    ({"roadmap_id": "zzz"}, "roadmap_id"),
    ({"reason": ""}, "reason"),
])
def test_refusals(tmp_path, monkeypatch, kw, needle):
    monkeypatch.setattr(op, "main_worktree_root", lambda r: tmp_path)
    _setup(tmp_path)
    out = _call(tmp_path, **kw)
    assert out["exit_code"] == 1 and needle in out["error"]


@pytest.mark.parametrize("extra,needle", [
    ({"deliverable_id": "dlv-x"}, "deliverable_id"),
    ({"governing_plan": "docs/plans/other.md"}, "governing_plan"),
    ({"plan_ids": "[pln-a]"}, "plan_ids"),
])
def test_existing_link_is_named_and_not_overwritten(tmp_path, monkeypatch, extra, needle):
    monkeypatch.setattr(op, "main_worktree_root", lambda r: tmp_path)
    _setup(tmp_path, **extra)
    before = (tmp_path / "state/handoffs/2026-10-21_eet-22.md").read_text(encoding="utf-8")
    out = _call(tmp_path)
    assert out["exit_code"] == 1 and needle in out["error"]
    assert (tmp_path / "state/handoffs/2026-10-21_eet-22.md").read_text(encoding="utf-8") == before
