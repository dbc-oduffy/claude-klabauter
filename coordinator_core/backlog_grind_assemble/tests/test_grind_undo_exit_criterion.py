"""Exit criterion for the grind undo path: the command the undo stage renders restores a
verify-failed row to HEAD, and the destructive-action guard still denies raw git checkout."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from urllib.parse import quote

import pytest

from coordinator_core.backlog_grind_assemble import grind_rows
from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.ops.dispatch_emit import grind_stages
from coordinator_core.session import scope

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "undo-exit-criterion-session"
AGENT_TYPE = "coordinator:queue-grind-op-runner"


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def _tree(repo):
    return {
        p.relative_to(repo).as_posix(): p.read_bytes()
        for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    }


def _js_encode_uri_component(s: str) -> str:
    return quote(s, safe="-_.!~*'()").replace("'", "%27")


def _rendered_command(paths):
    """The stage's shell command, with the JS encoder expression evaluated in Python."""
    js = grind_stages.compose_undo_call(label="undo", phase_title="Undo", paths=paths)
    match = re.search(r"JSON\.stringify\((\{paths: \[.*?\]\})\)\)\.replace", js)
    assert match, js
    payload = json.loads(match.group(1).replace("{paths:", '{"paths":'))
    token = _js_encode_uri_component(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    assert "'" not in token
    return f"{grind_stages.ASSEMBLE_CMD} grind-row undo --paths-urlenc '{token}' --repo-root REPO"


def _run_as_op_runner(command, repo):
    argv = shlex.split(command)
    assert argv[1:3] == ["grind-row", "undo"], argv
    return grind_rows.main(["undo", *[a if a != "REPO" else str(repo) for a in argv[3:]]])


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "core.autocrlf", "false")
    (r / "fixed.md").write_text("original fixed\n")
    (r / "row one.md").write_text("row one\n")
    (r / "unclaimed.md").write_text("unclaimed\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "seed")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SID)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.chdir(r)
    return r


def test_verify_failed_row_restored_by_stage_command(repo, capsys):
    head_tree = _tree(repo)
    (repo / "fixed.md").write_text("bad fix\n")
    (repo / "row one.md").unlink()
    (repo / "archive").mkdir()
    (repo / "archive" / "row one's copy.md").write_text("archived\n")
    (repo / "new.md").write_text("new\n")
    paths = ["fixed.md", "row one.md", "archive/row one's copy.md", "new.md"]
    for p in paths:
        scope.touch(SID, p, cwd=str(repo), root=str(repo))

    assert _run_as_op_runner(_rendered_command(paths), repo) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["restored"] == ["fixed.md", "row one.md"]
    assert out["removed"] == ["archive/row one's copy.md", "new.md"]
    assert _tree(repo) == head_tree
    assert not (repo / "new.md").exists()
    assert not (repo / "archive" / "row one's copy.md").exists()


@pytest.mark.parametrize("tool_name", ["Bash", "PowerShell"])
def test_guard_still_denies_checkout_on_undeclared_path(tool_name):
    payload = {
        "tool_name": tool_name,
        "tool_input": {"command": "git checkout HEAD -- unclaimed.md"},
        "session_id": "sess1",
        "cwd": None,
        "agent_id": "deadbeef0123",
        "agent_type": AGENT_TYPE,
    }
    assert guard.check(payload) is not None
