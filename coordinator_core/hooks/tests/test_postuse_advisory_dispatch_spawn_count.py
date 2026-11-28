"""Exact-equality spawn count for `hooks.postuse_advisory_dispatch`.

The Bash-commit claim-release leg compares the index to HEAD and starts no process. A Write
whose path the zero-spawn normalizer cannot prove (non-ASCII) pays one `git ls-files` in the
touch-record leg. The figures are read from the budget manifest, never chosen here.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.hooks import postuse_advisory_dispatch as pad
from coordinator_core.win_portability import no_console_creationflags
from pathlib import Path

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo, *args) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )


def _budget() -> dict:
    return load_manifest()["overrides"]["hooks.postuse_advisory_dispatch"]["spawn_count_budget"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    return root


def _fire(repo, monkeypatch, params):
    monkeypatch.setenv("CLAUDE_HOME", str(repo.parent / "claude-home"))
    with _count_spawns_attributed(monkeypatch) as spawns:
        asyncio.run(pad._handler(params, repo_root=repo / ".git"))
    return spawns


_IO_EVENTS = ("open", "os.listdir", "os.scandir")
_io_active = False
_io_count = 0


def _io_hook(event: str, args: tuple) -> None:
    global _io_count
    if _io_active and event in _IO_EVENTS:
        _io_count += 1


sys.addaudithook(_io_hook)

# Steady-call file opens per payload shape: the context-window sidecar, plus for an Edit the
# session meta and the edited file. Any further per-call read is a regression.
@pytest.mark.parametrize(
    ("params", "bound"),
    [
        ({"session_id": "sid-io", "tool_name": "Bash", "tool_input": {"command": "echo hi"}}, 1),
        ({"session_id": "sid-io", "tool_name": "Edit", "file_path": "plain.py"}, 3),
        ({"session_id": "sid-io", "tool_name": "Read", "file_path": "plain.py"}, 1),
    ],
    ids=["bash", "edit", "read"],
)
def test_a_steady_call_opens_at_most_the_bounded_file_count(
    repo, monkeypatch: pytest.MonkeyPatch, params, bound
) -> None:
    global _io_active, _io_count
    monkeypatch.setenv("CLAUDE_HOME", str(repo.parent / "claude-home"))
    (repo / "plain.py").write_text("x = 1\n", encoding="utf-8")
    params = {**params, **({"file_path": str(repo / "plain.py")} if "file_path" in params else {})}
    asyncio.run(pad._handler(params, repo_root=repo / ".git"))
    _io_count = 0
    _io_active = True
    try:
        asyncio.run(pad._handler(params, repo_root=repo / ".git"))
    finally:
        _io_active = False
    assert _io_count <= bound, _io_count


def test_ascii_write_touch_spawns_nothing(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "plain.py").write_text("x = 1\n", encoding="utf-8")
    spawns = _fire(
        repo,
        monkeypatch,
        {"session_id": "sid-ascii", "tool_name": "Write", "file_path": str(repo / "plain.py")},
    )
    assert len(spawns) == _budget()["ascii_write_touch"], [s.argv for s in spawns]


def test_non_ascii_write_touch_spawns_exactly_the_budgeted_ls_files(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "café.py").write_text("x = 1\n", encoding="utf-8")
    spawns = _fire(
        repo,
        monkeypatch,
        {"session_id": "sid-nonascii", "tool_name": "Write", "file_path": str(repo / "café.py")},
    )
    assert len(spawns) == _budget()["non_ascii_write_touch"], [s.argv for s in spawns]
    assert [s.origin for s in spawns] == ["run_git"] * len(spawns)
    assert all("ls-files" in s.argv for s in spawns), [s.argv for s in spawns]
