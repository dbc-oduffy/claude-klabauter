"""
coordinator_core.tests.test_pickup_brief_task_recreate

Purpose: pins that `pickup_brief.brief` at default arguments turns a baton's
`yaml tasks` block into `harness_task_directives`, leaves `directives` and
`judgment_points` untouched, and writes nothing when the list is empty.

Run from the repo root: python -m pytest
coordinator_core/tests/test_pickup_brief_task_recreate.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.pickup_brief as pb
from coordinator_core import task_carry
from coordinator_core.pickup_assemble.tests._git_harness import git as _git, init_repo as _init_repo
from coordinator_core.session import record_homes

# Declared, not excused: `brief()` reads real git state (tree quiescence,
# premise drift) that no fixture stands in for.
_H = record_homes.record_path("", "handoffs", "h.md").replace("\\", "/")

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_TASKS = [
    {"id": "1", "subject": "First", "description": "do one", "status": "in_progress", "blockedBy": []},
    {"id": "2", "subject": "Second", "description": "do two", "status": "pending", "blockedBy": ["1"]},
]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    reg_dir = tmp_path / "registry"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    r = tmp_path / "repo"
    r.mkdir()
    _init_repo(r)
    return r


def _seed(repo: Path, body: str) -> None:
    path = Path(record_homes.record_path(str(repo), "handoffs", "h.md"))
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        'title: "T"\ncreated: 2026-01-01\nbranch: work/test/2026-01-01\nstatus: open\n'
        'predecessor: "none"\ndeployment_state: awaiting_gate\npickup_ready: true\n'
    )
    path.write_text(f"---\n{fm}---\n\n# H\n\n{body}\n", encoding="utf-8")
    _git(repo, "add", _H)
    _git(repo, "commit", "-m", "add baton")


def test_carried_tasks_become_directives(repo: Path) -> None:
    _seed(repo, task_carry.render_tasks_section(_TASKS))
    obj = pb.brief(_H, repo_root=repo).decision_object
    steps = obj["harness_task_directives"]
    assert [(s["tool"], s["old_id"]) for s in steps] == [
        ("TaskCreate", "1"),
        ("TaskCreate", "2"),
        ("TaskUpdate", "1"),
        ("TaskUpdate", "2"),
    ]
    assert steps[0]["args"] == {"subject": "First", "description": "do one"}
    assert steps[2]["args"] == {"status": "in_progress"}
    assert steps[3]["args"] == {"addBlockedBy": ["1"]}
    assert "Recreate 2 carried tasks via harness_task_directives." in obj["next_move"]
    assert all(d.get("tool") is None for d in obj["directives"])


def test_empty_list_writes_nothing_and_leaves_next_move(repo: Path) -> None:
    _seed(repo, "No tasks carried.")
    obj = pb.brief(_H, repo_root=repo).decision_object
    assert obj["harness_task_directives"] == []
    assert "harness_task_directives" not in obj["next_move"]


def test_non_handoff_artifact_gets_empty_list(repo: Path) -> None:
    memo = Path(record_homes.record_path(str(repo), "cross-repo", "m.md"))
    memo.parent.mkdir(parents=True, exist_ok=True)
    memo.write_text(
        '---\ntitle: "M"\nfrom: a-em\nto: b-em\ncreated: 2026-01-01\nstatus: open\n---\n\n'
        + task_carry.render_tasks_section(_TASKS)
        + "\n",
        encoding="utf-8",
    )
    rel = record_homes.record_path("", "cross-repo", "m.md").replace("\\", "/")
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", "add memo")
    obj = pb.brief(rel, repo_root=repo).decision_object
    assert obj["harness_task_directives"] == []
