"""A general-purpose subagent's scoped pathspec `git -C <repo> add|commit` of new files is allowed.

Drives the real PreToolUse Bash dispatcher entry (`dispatch.evaluate_payload_json`) with a
general-purpose subagent payload against a example-game-repo-like repo built under `tmp_path`. The
`-C <repo-root>` spelling is the shape `block_subagent_destructive_action._scratchpad_repo_verdict`
denies ("git -C add|commit (only into a repo under the session scratchpad)") with the
`ceremony.commit_v2 --params-file` route and "needs authorizing"; plain `git add` / `git commit`
from inside the repo is not routed through it, which is why the same files committed earlier
without `-C` passed. Genuinely sweeping or history-rewriting shapes stay refused.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from coordinator_core.bash_guards import dispatch

pytestmark = pytest.mark.complete

_AGENT_ID = "a341f949fca0558d6"
_SESSION_ID = "c1c1c1c1-0000-4000-8000-00000000e889"
_NEW_FILES = ("docs/new_one.md", "Source/NewTwo.cpp")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        creationflags=_NO_WINDOW,
    )


@pytest.fixture
def example_game_repo_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "example-game-workbench-repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "Source").mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True, creationflags=_NO_WINDOW)
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")
    for rel in _NEW_FILES:
        (repo / rel).write_text("new\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    return repo


def _verdict(command: str, cwd: Path, agent_type: str = "general-purpose") -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": _SESSION_ID,
        "cwd": str(cwd),
        "agent_id": _AGENT_ID,
        "agent_type": agent_type,
    }
    return dispatch.evaluate_payload_json(json.dumps(payload))  # type: ignore[return-value]


def _decision(out: Optional[Dict[str, Any]]) -> str:
    if not out:
        return "allow"
    return out["hookSpecificOutput"].get("permissionDecision", "allow")


def _reason(out: Optional[Dict[str, Any]]) -> str:
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


def _root(repo: Path) -> str:
    return str(repo).replace("\\", "/")


@pytest.mark.parametrize("cwd_is_repo", [True, False], ids=["cwd-in-repo", "cwd-elsewhere"])
@pytest.mark.parametrize(
    "template",
    [
        "git -C {root} add " + " ".join(_NEW_FILES),
        "git -C {root} commit -m msg -- " + " ".join(_NEW_FILES),
    ],
    ids=["add-new-files", "commit-new-files"],
)
def test_subagent_dash_c_scoped_pathspec_of_new_files_is_allowed(
    example_game_repo_repo: Path, template: str, cwd_is_repo: bool
) -> None:
    cwd = example_game_repo_repo if cwd_is_repo else example_game_repo_repo.parent
    out = _verdict(template.format(root=_root(example_game_repo_repo)), cwd)
    assert _decision(out) != "deny", _reason(out)
    assert "ceremony.commit_v2" not in _reason(out)


@pytest.mark.parametrize(
    "command",
    ["git add " + " ".join(_NEW_FILES), "git commit -m msg -- " + " ".join(_NEW_FILES)],
    ids=["plain-add", "plain-commit"],
)
def test_subagent_plain_scoped_pathspec_of_new_files_is_allowed(example_game_repo_repo: Path, command: str) -> None:
    out = _verdict(command, example_game_repo_repo)
    assert _decision(out) != "deny", _reason(out)


@pytest.mark.parametrize(
    "template",
    [
        "git -C {root} add .",
        "git -C {root} add -A",
        "git -C {root} add --all docs/new_one.md",
        "git -C {root} add -u",
        "git -C {root} add --update docs/new_one.md",
        "git -C {root} add -p docs/new_one.md",
        "git -C {root} add docs/new_one.md .",
        "git -C {root} add :/",
        "git -C {root} add ':(glob)**/*.md'",
        "git -C {root} add *",
        "git -C {root} add",
        "git -C {root} add docs/",
        "git -C {root} commit -a -m msg -- docs/new_one.md",
        "git -C {root} commit --all -m msg -- docs/new_one.md",
        "git -C {root} commit -am msg -- docs/new_one.md",
        "git -C {root} commit --amend -m msg -- docs/new_one.md",
        "git -C {root} push --force origin main",
    ],
    ids=[
        "add-dot", "add-all", "add-all-long", "add-u", "add-update", "add-patch", "add-dot-operand",
        "add-root-magic", "add-pathspec-magic", "add-star", "add-bare", "add-directory",
        "commit-a", "commit-all", "commit-am", "commit-amend", "push-force",
    ],
)
def test_subagent_sweeping_or_irreversible_dash_c_shapes_stay_refused(example_game_repo_repo: Path, template: str) -> None:
    out = _verdict(template.format(root=_root(example_game_repo_repo)), example_game_repo_repo)
    assert _decision(out) == "deny", out
