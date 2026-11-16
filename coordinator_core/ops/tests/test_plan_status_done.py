"""
coordinator_core/ops/tests/test_plan_status_done.py — the "plan.status_done" op.

Subject: `coordinator_core.ops.plan_status_done`. The handler is called directly over `tmp_path`
fixtures (a bare tree with a `.git` DIRECTORY); the flip is the real `stamp-implemented` stamper.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import plan_status_done as mod
from coordinator_core.ops.plan_tasks_mutate import EM_DISCHARGE_KEY, evidence_sidecar_path


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    return tmp_path


def _row(row_id: str, disposition: str) -> str:
    return "\n".join(
        [
            f"- id: {row_id}",
            "  title: Ship it",
            "  body: Do the named work.",
            "  change_kind: code-edit",
            "  surface: a.py",
            "  writes: [a.py]",
            "  queue_scope: project",
            f"  disposition: {disposition}",
        ]
    )


def _plan(root: Path, rows: list, status: str = "executing", name: str = "a.md") -> str:
    (root / "docs" / "plans" / name).write_text(
        f"---\ntitle: fixture\nstatus: {status}\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        + "\n".join(rows)
        + "\n```\n",
        encoding="utf-8",
    )
    return f"docs/plans/{name}"


_FULL = {"recorded_at": "2026-11-16T00:00:00Z", "text": "done", "commit": "a" * 40, "evidence": "landed"}


def _status(root: Path, plan: str) -> str:
    return yaml.safe_load((root / plan).read_text(encoding="utf-8").split("---")[1])["status"]


def test_every_row_closed_moves_executing_to_implemented(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root, [_row("C1", "coded"), _row("C2", "delivered_elsewhere"), _row("C3", "wont_do")])
    reply = mod._handler({"plan": plan}, root)
    assert (reply["plan"], reply["prior_status"], reply["status"], reply["stamped"]) == (
        plan,
        "executing",
        "implemented",
        True,
    )
    assert _status(root, plan) == "implemented"


def test_em_discharged_row_counts_closed(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root, [_row("C1", "coded"), _row("C2", "open")])
    evidence_sidecar_path(root / plan).write_text(
        yaml.safe_dump({EM_DISCHARGE_KEY: {"C2": _FULL}}), encoding="utf-8"
    )
    assert mod._handler({"plan": plan}, root)["status"] == "implemented"
    assert _status(root, plan) == "implemented"


def test_open_row_refuses_naming_it_and_leaves_the_file_byte_identical(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root, [_row("C1", "coded"), _row("C2", "open"), _row("C3", "open")])
    before = (root / plan).read_bytes()
    with pytest.raises(ValueError, match=r"2 spine row\(s\) still open: C2, C3"):
        mod._handler({"plan": plan}, root)
    assert (root / plan).read_bytes() == before


def test_undischarged_row_beside_a_discharged_one_still_refuses(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root, [_row("C1", "open"), _row("C2", "open")])
    evidence_sidecar_path(root / plan).write_text(
        yaml.safe_dump({EM_DISCHARGE_KEY: {"C1": _FULL}}), encoding="utf-8"
    )
    before = (root / plan).read_bytes()
    with pytest.raises(ValueError, match="C2"):
        mod._handler({"plan": plan}, root)
    assert (root / plan).read_bytes() == before


def test_already_implemented_plan_is_a_no_op(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root, [_row("C1", "coded")], status="implemented")
    reply = mod._handler({"plan": plan}, root)
    assert (reply["stamped"], reply["status"]) == (False, "implemented")


@pytest.mark.parametrize(
    "params, message",
    [({}, "non-empty plan path"), ({"plan": "  "}, "non-empty plan path"), ({"plan": "docs/plans/none.md"}, "no such plan")],
)
def test_bad_params_and_missing_plan_raise(tmp_path, params, message):
    with pytest.raises(ValueError, match=message):
        mod._handler(params, _repo(tmp_path))


def test_plan_outside_the_worktree_is_refused(tmp_path):
    (tmp_path / "repo").mkdir()
    root = _repo(tmp_path / "repo")
    outside = tmp_path / "outside.md"
    outside.write_text("---\nstatus: executing\n---\n", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        mod._handler({"plan": str(outside)}, root)
    assert outside.read_text(encoding="utf-8") == "---\nstatus: executing\n---\n"


def test_no_repo_root_raises(tmp_path):
    with pytest.raises(ValueError, match="repo_root"):
        mod._handler({"plan": "x.md"}, None)
