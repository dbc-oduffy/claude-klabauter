"""plan.backfill_warp_executing + roadmap gate: a plan whose only open rows are withheld counts as coded."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops import plan_backfill_warp_executing as op
from coordinator_core.ops.dispatch_emit.spine_read import coded_or_withheld
from coordinator_core.roadmap import gate_v2
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

GATED = (
    "## Tasks\n\n```yaml plan-tasks\n"
    "- id: C1\n  title: a\n  disposition: coded\n  disposition_ref: abc1234\n"
    "- id: C9\n  title: b\n  disposition: open\n"
    "  external_gate:\n    - id: g\n      owner_repo: r\n      blocks: execution\n```\n"
)
SLOT = (
    "## Tasks\n\n```yaml plan-tasks\n"
    "- id: C1\n  title: a\n  disposition: coded\n  disposition_ref: abc1234\n"
    "- id: C12\n  title: b\n  disposition: open\n  needs_slot: true\n```\n"
)
UNGATED = (
    "## Tasks\n\n```yaml plan-tasks\n"
    "- id: C1\n  title: a\n  disposition: coded\n  disposition_ref: abc1234\n"
    "- id: C2\n  title: b\n  disposition: open\n```\n"
)
CLEARED = GATED.replace("blocks: execution", "blocks: execution\n      cleared: true")


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _plan(root, name, status, spine):
    path = root / "docs/plans" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {name}\nplan_id: {name}\nstatus: {status}\ndeliverable_id: d-{name}\n---\n\n{spine}",
        encoding="utf-8",
    )


def _baton(root, name, **extra):
    d = root / "state/handoffs"
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"stub_id: {name}", "baton_role: work", "status: open", "deployment_state: ready_to_fire"]
    lines += [f"{k}: {v}" for k, v in extra.items()]
    (d / f"{name}.md").write_text("---\n" + "\n".join(lines) + "\n---\nbody\n", encoding="utf-8")


def _manifest(root, plan_id):
    d = root / "state/warp" / f"run-{plan_id}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({"plan_id": plan_id, "rows": []}), encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "00000000-0000-4000-8000-000000000000")
    _git(tmp_path, "init", "-q", "-b", "main")
    return tmp_path


def _build(repo, specs):
    for name, status, spine in specs:
        _plan(repo, name, status, spine)
        _manifest(repo, name)
    _baton(repo, "eet-01", deliverable_id="d-eet-01")
    _baton(repo, "eet-02", deliverable_id="d-eet-02")
    _baton(repo, "dep", blocked_by="[eet-01, eet-02]")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")


def _dep_gate(repo):
    row = next(b for b in gate_v2.compute_gate_report(repo)["batons"] if b["id"] == "dep")
    return row["execution_gate"]


def test_withheld_plans_flip_to_landed_and_open_the_dependents_execution_gate(repo):
    _build(repo, [("eet-01", "approved", GATED), ("eet-02", "executing", SLOT)])
    assert _dep_gate(repo)["open"] is False

    dry = op._handler({}, repo_root=repo / ".git")
    assert {c["plan_id"]: c["target_status"] for c in dry["candidates"]} == {"eet-01": "landed", "eet-02": "landed"}
    assert _dep_gate(repo)["open"] is False

    out = op._handler({"apply": True}, repo_root=repo / ".git")
    assert len(out["applied"]) == 2 and out["commit_error"] is None
    for name in ("eet-01", "eet-02"):
        assert "status: landed\n" in (repo / "docs/plans" / f"{name}.md").read_text(encoding="utf-8")
    assert _dep_gate(repo) == {"open": True, "blocking": []}


def test_a_plan_with_an_open_ungated_row_does_not_flip_to_landed(repo):
    _build(repo, [("eet-01", "approved", UNGATED), ("eet-02", "approved", CLEARED)])
    out = op._handler({"apply": True}, repo_root=repo / ".git")
    assert {c["plan_id"]: c["target_status"] for c in out["candidates"]} == {
        "eet-01": "executing", "eet-02": "executing",
    }
    for name in ("eet-01", "eet-02"):
        assert "status: executing\n" in (repo / "docs/plans" / f"{name}.md").read_text(encoding="utf-8")
    assert _dep_gate(repo)["open"] is False


def test_executing_plan_with_an_ungated_open_row_is_left_alone(repo):
    _build(repo, [("eet-01", "executing", UNGATED)])
    assert op._handler({}, repo_root=repo / ".git")["candidates"] == []


def test_coded_or_withheld_truth_table():
    assert coded_or_withheld(GATED) and coded_or_withheld(SLOT)
    assert not coded_or_withheld(UNGATED) and not coded_or_withheld(CLEARED)
    assert not coded_or_withheld("no spine here")
    only_withheld = SLOT.replace("disposition: coded\n  disposition_ref: abc1234", "disposition: open\n  needs_slot: true")
    assert not coded_or_withheld(only_withheld)


def test_close_path_lands_only_a_withheld_shape():
    from coordinator_core.ops.ceremony.close_background import _land_if_withheld

    def doc(status, spine):
        return f"---\ntitle: t\nstatus: {status}\n---\n\n{spine}"

    assert "status: landed\n" in _land_if_withheld(doc("approved", GATED))
    assert "status: landed\n" in _land_if_withheld(doc("executing", SLOT))
    assert _land_if_withheld(doc("approved", UNGATED)) == doc("approved", UNGATED)
    assert _land_if_withheld(doc("implemented", GATED)) == doc("implemented", GATED)
