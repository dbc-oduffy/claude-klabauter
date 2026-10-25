"""Declared-delete marker form through the whole registered guard chain (fixtures under tmp_path)."""

from __future__ import annotations

import json
import statistics
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.bash_guards.dispatch import evaluate_payload_json

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-2026-10-25-demo-20261025T000000-abcdef"
ROW = "C5"
SESSION = "sess-emitter"
MARK = f"COORDINATOR_DISPATCH_ROW={RUN}/{ROW}"
DECLARED = "pkg/a.py"
UNDECLARED = "pkg/b.py"


def _write_run(root: Path) -> None:
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT

    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    manifest = {
        "run_dir": str(rec),
        "rows": [{"id": ROW, "agent_type": "x", "model": "m", "brief_path": "b.md", "writes": [DECLARED], "wave": 0}],
        "review_declared_paths": [],
        "marker_path": "m",
    }
    (rec / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    run_dir = root / RUN_DIR_ROOT
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{RUN}.workflow.mjs.emitted.json").write_text(json.dumps({"session_id": SESSION}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _roster_resolves(monkeypatch):
    """The reviewer-allowlist guard's roster leg reads the real DoE checkout; pin it so the verdict is host-independent."""
    from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist as reviewer_guard

    monkeypatch.setattr(reviewer_guard, "is_confined_by_roster_absence", lambda t: False)


@pytest.fixture(autouse=True)
def _channel_live(monkeypatch):
    monkeypatch.setattr(guard, "_DECLARED_DELETE_CHANNEL_LIVE", True)


@pytest.fixture
def repo(tmp_path):
    nowin = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, creationflags=nowin)
    (tmp_path / "pkg").mkdir()
    (tmp_path / DECLARED).write_text("x", encoding="utf-8")
    (tmp_path / UNDECLARED).write_text("y", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "pkg"], check=True, creationflags=nowin)
    _write_run(tmp_path)
    return tmp_path


def _payload(repo: Path, command: str) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": SESSION,
        "cwd": str(repo),
        "env": {},
        "agent_id": "deadbeef0123",
        "agent_type": "coordinator:executor",
    }


def _chain(repo: Path, command: str):
    return evaluate_payload_json(json.dumps(_payload(repo, command)))


def _denied(out) -> bool:
    return (
        isinstance(out, dict)
        and isinstance(out.get("hookSpecificOutput"), dict)
        and out["hookSpecificOutput"].get("permissionDecision") == "deny"
    )


def _reason(out) -> str:
    return json.dumps(out)


@pytest.mark.parametrize(
    "command",
    [f"{MARK} git rm -q -- {DECLARED}", f"{MARK} rm -- {DECLARED}"],
)
def test_declared_tracked_file_admitted_by_every_guard(repo, command):
    out = _chain(repo, command)
    assert not _denied(out), out


def test_undeclared_git_rm_denied_by_destructive_action_guard(repo):
    out = _chain(repo, f"{MARK} git rm -q -- {UNDECLARED}")
    assert _denied(out), out
    assert "block-subagent-destructive-action" in _reason(out)


def test_declared_git_rm_costs_the_guard_no_spawn_and_under_50ms(repo, monkeypatch):
    command = f"{MARK} git rm -q -- {DECLARED}"
    payload = _payload(repo, command)
    assert guard.check(payload) is None

    spawns = []
    real_popen = subprocess.Popen

    def counting_popen(*a, **k):
        spawns.append(a)
        return real_popen(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", counting_popen)
    samples = []
    for _ in range(50):
        t0 = time.process_time()
        result = guard.check(payload)
        samples.append(time.process_time() - t0)
        assert result is None
    assert spawns == []
    assert statistics.median(samples) < 0.05
