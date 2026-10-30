"""A session whose fired run has landed or halted edits and commits freely; a live run's owned
paths still produce the advisory record. Drives `nudge_em_code_dispatch.op` and the Bash guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.bash_guards import guard_em_post_fire_shell_write as bash_guard
from coordinator_core.hooks import _em_post_fire as pf
from coordinator_core.hooks import _em_write_budget as leaf
from coordinator_core.hooks import flag_em_poll_in_flight as fl
from coordinator_core.hooks import nudge_em_code_dispatch as mod
from coordinator_core.ops.dispatch_emit import halted_run

SID = "sess-unlock"
OWNED = "pkg/owned.py"
FREE = "pkg/free.py"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "pkg").mkdir()
    for target in (mod, leaf):
        monkeypatch.setattr(target, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
    # Trap: tmp_path lives under the system temp dir, which `_is_scratch_or_tmp` classifies as
    # scratch, so without this no fixture path is ever product.
    monkeypatch.setattr(leaf, "_is_scratch_or_tmp", lambda file_path: False)
    monkeypatch.setattr(fl, "session_workflow_run_ids", lambda sid: ["run-1"])
    run_dir = root / "state" / "warp" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [OWNED]}], "doc_writes": []}), encoding="utf-8")
    return root


def _end(root: Path, status: str) -> None:
    (root / "state" / "warp" / "run-1" / halted_run.HALTED_FILE).write_text(
        json.dumps({"status": status}), encoding="utf-8")


def _edit(root: Path, rel: str) -> dict:
    return {
        "tool_name": "Edit",
        "session_id": SID,
        "cwd": str(root),
        "tool_input": {"file_path": str(root / rel), "old_string": "a = 1\n", "new_string": "a = f(1)\n"},
    }


def _shell_hits(monkeypatch, root: Path, cmd: str) -> int:
    hits = []
    monkeypatch.setattr(pf, "deny_envelope", lambda *a, **k: hits.append(1))
    assert bash_guard.check(cmd, SID, str(root), {"cwd": str(root)}) is None
    return len(hits)


def _post_fire_recorded(result) -> bool:
    return bool(result) and "workflow.revise" in result["hookSpecificOutput"].get("additionalContext", "")


def _no_deny(result) -> bool:
    return not (result and result["hookSpecificOutput"].get("permissionDecision") == "deny")


def test_live_run_owned_path_is_recorded_not_denied(repo):
    result = mod.op(_edit(repo, OWNED))
    assert _no_deny(result)
    assert _post_fire_recorded(result)


def test_live_run_does_not_guard_a_path_it_does_not_own(repo):
    result = mod.op(_edit(repo, FREE))
    assert _no_deny(result) and not _post_fire_recorded(result)


@pytest.mark.parametrize("status", ["landed", "halted"])
def test_ended_run_releases_its_owned_path_for_edit(repo, status):
    _end(repo, status)
    result = mod.op(_edit(repo, OWNED))
    assert _no_deny(result) and not _post_fire_recorded(result)


def test_live_run_owned_shell_write_is_recorded(repo, monkeypatch):
    assert _shell_hits(monkeypatch, repo, f"cp /dev/null {OWNED}") == 1


def test_live_run_free_shell_write_is_not_recorded(repo, monkeypatch):
    assert _shell_hits(monkeypatch, repo, f"cp /dev/null {FREE}") == 0


@pytest.mark.parametrize("status", ["landed", "halted"])
def test_ended_run_releases_shell_write_and_merge_commit(repo, monkeypatch, status):
    _end(repo, status)
    (repo / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
    assert _shell_hits(monkeypatch, repo, f"cp /dev/null {OWNED}") == 0
    assert _shell_hits(monkeypatch, repo, "git commit -m 'merge peer'") == 0
    assert _shell_hits(monkeypatch, repo, "git merge --continue") == 0
