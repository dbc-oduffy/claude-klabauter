"""A met item whose declared path a commit already delivered is not refused pre-existing-dirt for foreign
worktree dirt in it, at the terminal-commit ledger check and the land_run gate; a dirty path no commit
delivered since the plan was created still refuses."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit import land_run, measured_rev, own_checkpoint_paths, requirements_ledger
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import ManifestRow, StageManifest
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN = "docs/plans/2026-11/p.md"
DELIVERED = "delivered.py"
UNDELIVERED = "undelivered.py"
LEDGER = SimpleNamespace(items=(1,), all_of_pm_ask=False)
VERDICT = {"items": [{"id": "R1", "status": "met"}]}
OLD = "2020-01-01T00:00:00 +0000"


def _git(root: Path, *args: str, date: str | None = None) -> None:
    env = {**os.environ, **({"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date} if date else {})}
    subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, env=env,
        **no_console_creationflags(),
    )


def _put(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _put(tmp_path, PLAN, "---\ncreated: 2026-11-15\n---\n# p\n")
    _put(tmp_path, DELIVERED, "v0\n")
    _put(tmp_path, UNDELIVERED, "v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "old history", date=OLD)
    _put(tmp_path, DELIVERED, "v1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "delivery")
    for rel in (DELIVERED, UNDELIVERED):
        _put(tmp_path, rel, "v1\nfoo\n")
    run = tmp_path / "state" / "warp" / "warp-x-aaaaaa-20261112T000000-bbbbbb"
    run.mkdir(parents=True)
    return tmp_path, run


def _terminal(root, run, paths):
    row = ManifestRow(
        id="C1", agent_type="x", model="m", brief_path="b", writes=tuple(paths), wave=0, register_rows=("R1",),
    )
    m = StageManifest(
        run_dir="r", rows=(row,), review_declared_paths=(), marker_path="m", write_baseline=wb.capture(root, paths),
    )
    return tc._pre_existing_dirt_refusal(root, m, VERDICT, LEDGER, PLAN, run)


def _land(root, monkeypatch, run, paths):
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({
        "write_baseline": wb.capture(root, paths),
        "rows": [{"id": "C1", "register_rows": ["R1"], "writes": list(paths)}],
    }))
    monkeypatch.setattr(requirements_ledger, "read_ledger", lambda *_a, **_k: SimpleNamespace(items=[], all_of_pm_ask=False))
    monkeypatch.setattr(requirements_ledger, "judge", lambda *_a, **_k: {"pass": True, "failures": [], "open": []})
    monkeypatch.setattr(requirements_ledger, "noted_paths", lambda *_a, **_k: [])
    monkeypatch.setattr(measured_rev, "measured_rev_drift", lambda *_a, **_k: [])
    monkeypatch.setattr(own_checkpoint_paths, "checkpoint_landed_paths", lambda *_a, **_k: frozenset())
    return land_run._ledger_gate(
        root, run, {"items": [{"id": "R1", "status": "met"}]}, manifest, [], {}, None,
        {"prep": {"run_base_sha": "abc"}}, None, PLAN,
    )[0]


def test_helper_credits_only_the_path_a_commit_changed_since_the_plan_floor(repo):
    root, _ = repo
    assert wb.delivered_at_head(root, PLAN, [DELIVERED, UNDELIVERED]) == {DELIVERED}
    assert wb.delivered_at_head(root, None, [DELIVERED]) == frozenset()
    assert wb.delivered_at_head(root, "docs/plans/absent.md", [DELIVERED]) == frozenset()


def test_helper_walk_is_capped_at_the_commit_budget(repo, monkeypatch):
    root, _ = repo
    _put(root, "other.py", "x\n")
    _git(root, "add", "other.py")
    _git(root, "commit", "-q", "-m", "later")
    assert wb.delivered_at_head(root, PLAN, [DELIVERED]) == {DELIVERED}
    monkeypatch.setattr(wb, "_DELIVERY_WALK_CAP", 1)
    assert wb.delivered_at_head(root, PLAN, [DELIVERED]) == frozenset()


def test_terminal_check_does_not_hold_a_delivered_path_on_foreign_dirt(repo, monkeypatch):
    root, run = repo
    monkeypatch.setattr(tc, "_plan_checkpoint_touched", lambda *_a: frozenset())
    assert _terminal(root, run, [DELIVERED]) is None


def test_terminal_check_still_refuses_a_dirty_path_no_commit_delivered(repo, monkeypatch):
    root, run = repo
    monkeypatch.setattr(tc, "_plan_checkpoint_touched", lambda *_a: frozenset())
    out = _terminal(root, run, [DELIVERED, UNDELIVERED])
    assert out["ledger_failures"][0]["reason"] == "pre-existing-dirt: " + UNDELIVERED


def test_land_run_gate_does_not_hold_a_delivered_path_on_foreign_dirt(repo, monkeypatch):
    root, run = repo
    assert _land(root, monkeypatch, run, [DELIVERED]) is None


def test_land_run_gate_still_refuses_a_dirty_path_no_commit_delivered(repo, monkeypatch):
    root, run = repo
    out = _land(root, monkeypatch, run, [DELIVERED, UNDELIVERED])
    assert out is not None and out["refused"] == "ledger-unmet"
    assert "pre-existing-dirt: " + UNDELIVERED in json.dumps(out)
    assert "pre-existing-dirt: " + DELIVERED not in json.dumps(out)
