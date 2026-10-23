"""plan.tasks.mutate resolve: a memo-send row closes on a verified sent receipt present on disk."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.ops.plan_tasks_render import load_rows
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SENT = ".coordinator-local/memo-outbox/sent"

_PLAN = """\
---
title: "Memo Receipt Plan"
status: draft
schema_version: '1.2.0'
---

# Memo Receipt Plan

## Tasks

```yaml plan-tasks
- id: M1
  title: Send the memo
  change_kind: script-edit
  kind: memo-send
  surface: a.py
  receipt: alpha
  external_gate:
  - owner_repo: other
    condition: commit-in-owner-repo
  queue_scope: project
  deferred: false
  body: |
    Send.
- id: M2
  title: Send the second memo
  change_kind: script-edit
  kind: memo-send
  surface: b.py
  queue_scope: project
  deferred: false
  body: |
    Send.
- id: C1
  title: Plain row
  change_kind: script-edit
  surface: c.py
  queue_scope: project
  deferred: false
  body: |
    Plain.
```
"""


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, **no_console_creationflags())


def _fixture(tmp_path, committed=("alpha", "beta")):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "m.md", _PLAN)
    (repo / _SENT).mkdir(parents=True)
    for slug in ("alpha", "beta", "gamma"):
        (repo / _SENT / f"{slug}.md").write_text(f"sent {slug}\n", encoding="utf-8")
    _git(repo, "add", *(f"{_SENT}/{s}.md" for s in committed))
    _git(repo, "commit", "-m", "receipts")
    return repo, plan


def _resolve(repo, plan, resolves):
    return asyncio.run(_handler(
        {"verb": "resolve", "plan_path": str(plan), "resolves": resolves}, repo_root=repo / ".git"
    ))


def _memo(task_id, slug, **over):
    r = {
        "id": task_id,
        "disposition": "delivered_elsewhere",
        "disposition_ref": f"{_SENT}/{slug}.md",
        "disposition_detail": "sent to receiver",
        "delivered_via": "memo",
    }
    r.update(over)
    return r


def _rows(plan):
    return {r["id"]: r for r in load_rows(plan.read_text(encoding="utf-8")).rows}


def test_receipt_on_disk_resolves_and_clears_gate(tmp_path):
    repo, plan = _fixture(tmp_path)
    out = _resolve(repo, plan, [_memo("M1", "alpha")])
    assert out["exit_code"] == 0, out
    row = _rows(plan)["M1"]
    assert row["disposition"] == "delivered_elsewhere"
    gate = row["external_gate"][0]
    assert gate["cleared"] is True
    assert gate["closure_evidence"] == f"memo sent: {_SENT}/alpha.md on disk"
    assert any("memo receipt verified" in w for w in out.get("warnings", []))
    assert not any("not commit-proven" in w for w in out.get("warnings", []))


def test_untracked_gitignored_receipt_resolves(tmp_path):
    repo, plan = _fixture(tmp_path)
    (repo / ".gitignore").write_text(".coordinator-local\n", encoding="utf-8")
    out = _resolve(repo, plan, [_memo("M2", "gamma")])
    assert out["exit_code"] == 0, out
    assert _rows(plan)["M2"]["disposition"] == "delivered_elsewhere"


def test_missing_receipt_refuses(tmp_path):
    repo, plan = _fixture(tmp_path)
    before = plan.read_bytes()
    out = _resolve(repo, plan, [_memo("M2", "delta")])
    assert out["exit_code"] != 0
    assert "not on disk" in out["error"]
    assert "plan.tasks.mutate" in out["error"]
    assert plan.read_bytes() == before


def test_mismatched_slug_refuses(tmp_path):
    repo, plan = _fixture(tmp_path)
    out = _resolve(repo, plan, [_memo("M1", "beta")])
    assert out["exit_code"] != 0
    assert "row declares 'alpha'" in out["error"]
    assert "plan.tasks.mutate" in out["error"]


def test_non_receipt_ref_refuses(tmp_path):
    repo, plan = _fixture(tmp_path)
    out = _resolve(repo, plan, [_memo("M1", "alpha", disposition_ref="other-repo@abc1234")])
    assert out["exit_code"] != 0
    assert "sent receipt" in out["error"]


def test_delivered_via_other_than_memo_refuses(tmp_path):
    repo, plan = _fixture(tmp_path)
    out = _resolve(repo, plan, [_memo("M1", "alpha", delivered_via="direct-message")])
    assert out["exit_code"] != 0
    assert "delivered_via memo" in out["error"]


def test_receipt_check_spawns_no_ls_tree(tmp_path, monkeypatch):
    repo, plan = _fixture(tmp_path)
    with _count_spawns_attributed(monkeypatch) as spawns:
        out = _resolve(repo, plan, [_memo("M1", "alpha"), _memo("M2", "beta")])
    assert out["exit_code"] == 0, out
    assert not any("ls-tree" in s.argv for s in spawns), [s.argv for s in spawns]


def test_non_memo_delivered_elsewhere_is_unchanged(tmp_path):
    repo, plan = _fixture(tmp_path)
    out = _resolve(repo, plan, [{
        "id": "C1",
        "disposition": "delivered_elsewhere",
        "disposition_ref": "other-repo@abc1234def",
        "disposition_detail": "landed elsewhere",
        "delivered_via": "direct-message",
    }])
    assert out["exit_code"] == 0, out
    assert any("completion asserted, not commit-proven" in w for w in out.get("warnings", []))


def test_em_discharge_refusal_names_the_route(tmp_path):
    repo, plan = _fixture(tmp_path)
    text = plan.read_text(encoding="utf-8").replace("  kind: memo-send\n  surface: a.py", "  kind: memo-send\n  performer: em\n  surface: a.py")
    plan.write_text(text, encoding="utf-8")
    out = asyncio.run(_handler(
        {"verb": "em-discharge", "plan_path": str(plan), "id": "M1", "text": "x"}, repo_root=repo / ".git"
    ))
    assert out["exit_code"] != 0
    assert f"{_SENT}/alpha.md" in out["error"]
