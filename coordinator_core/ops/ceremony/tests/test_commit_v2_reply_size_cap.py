"""`ceremony.commit_v2` replies stay bounded however many left-behind paths a commit leaves."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "rsc-self"
REPLY_BYTE_CAP = 8 * 1024
MANY = 600


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def test_cap_reply_keeps_a_sample_and_the_total():
    paths = [f"dir/file-{i:05d}.py" for i in range(MANY)]
    reply = {
        "committed": True,
        "own_left_behind": {"carried": [], "left": paths},
        "stopped_run_residue": {"carried": {"r1": paths}, "withheld": {}},
        "warnings": ["w"],
    }

    capped = commit_v2.cap_reply(reply)

    assert capped["own_left_behind"]["left"] == paths[: commit_v2.REPLY_LIST_CAP]
    assert capped["own_left_behind"]["left_total"] == MANY
    assert capped["stopped_run_residue"]["carried"]["r1"] == paths[: commit_v2.REPLY_LIST_CAP]
    assert capped["stopped_run_residue"]["carried_total"] == MANY
    assert capped["warnings"] == ["w"]
    assert "left_total" not in commit_v2.cap_reply({"own_left_behind": {"left": ["a"]}})["own_left_behind"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "a.py").write_text("v1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    (root / "a.py").write_text("v2\n", encoding="utf-8")
    session_core.init(SELF, cwd=str(root))
    session_scope.touch(SELF, "a.py", cwd=str(root))
    for i in range(MANY):
        name = f"left/{'x' * 40}-{i:05d}.py"
        (root / name).parent.mkdir(exist_ok=True)
        (root / name).write_text("n\n", encoding="utf-8")
        session_scope.touch(SELF, name, cwd=str(root))
    return root


def test_handler_reply_is_bounded_with_thousands_left_behind(repo):
    reply = commit_v2._handler({"paths": ["a.py"], "message": "m"}, repo_root=repo / ".git")

    assert reply["committed"] is True
    left = reply["own_left_behind"]
    assert left["left_total"] == MANY
    assert len(left["left"]) == commit_v2.REPLY_LIST_CAP
    assert len(json.dumps(reply)) < REPLY_BYTE_CAP


def test_full_reply_keeps_the_whole_set_for_terminal_commit(repo):
    reply = commit_v2._handler(
        {"paths": ["a.py"], "message": "m", "full_reply": True}, repo_root=repo / ".git"
    )

    assert len(reply["own_left_behind"]["left"]) == MANY
    assert "left_total" not in reply["own_left_behind"]
