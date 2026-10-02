"""check_validate_commit treats a lessons archival move (staged `state/lessons/<f>` deletion
paired with a staged `archive/lessons-archived/` write of the same basename) as touched, and
still refuses the unpaired deletion."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.bash_guards import dispatch as bash_dispatch
from coordinator_core.bash_guards.dispatch_checks import _lessons_archive_paired_paths
from coordinator_core.session import core
from coordinator_core.win_portability import no_console_creationflags


def _git(root: str, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, **no_console_creationflags()
    )


def _repo(tmp_path: Path) -> tuple[str, str]:
    root = str(tmp_path)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (tmp_path / "state/lessons").mkdir(parents=True)
    (tmp_path / "archive/lessons-archived").mkdir(parents=True)
    (tmp_path / "state/lessons/x.md").write_text("lesson x\n", encoding="utf-8")
    (tmp_path / "archive/lessons-archived/x.md").write_text("old\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    sid = "my-sess"
    assert core.init(sid, cwd=root)
    started = tmp_path / ".git" / "coordinator-sessions" / sid / "started_at"
    future = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + 3600, tz=timezone.utc)
    started.write_text(future.strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8")
    return root, sid


def _commit(root: str, sid: str):
    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": 'git commit -m "archive lesson"'},
        "session_id": sid,
        "cwd": root,
    })
    return bash_dispatch.evaluate_payload_json(payload)


def _text(result) -> str:
    if result is None:
        return ""
    out = result["hookSpecificOutput"]
    return out.get("permissionDecisionReason", "") + out.get("additionalContext", "")


def test_paired_helper_pairs_only_matching_basenames():
    lines = [
        "D\tstate/lessons/x.md",
        "M\tarchive/lessons-archived/x.md",
        "D\tstate/lessons/y.md",
    ]
    assert _lessons_archive_paired_paths(lines) == {"state/lessons/x.md"}


def test_paired_helper_pairs_rename_destination():
    lines = ["R100\tstate/lessons/x.md\tarchive/lessons-archived/x.md"]
    assert _lessons_archive_paired_paths(lines) == {"archive/lessons-archived/x.md"}


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_paired_deletion_is_not_denied_or_warned(monkeypatch, tmp_path):
    root, sid = _repo(tmp_path)
    (tmp_path / "state/lessons/x.md").unlink()
    (tmp_path / "archive/lessons-archived/x.md").write_text("old\nlesson x\n", encoding="utf-8")
    _git(root, "add", "-A")
    monkeypatch.setenv("COORDINATOR_SCOPE_STRICT", "1")
    text = _text(_commit(root, sid))
    assert "state/lessons/x.md" not in text


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_unpaired_deletion_still_flagged(monkeypatch, tmp_path):
    root, sid = _repo(tmp_path)
    (tmp_path / "state/lessons/x.md").unlink()
    _git(root, "add", "-A")
    monkeypatch.setenv("COORDINATOR_SCOPE_STRICT", "1")
    assert "state/lessons/x.md" in _text(_commit(root, sid))
