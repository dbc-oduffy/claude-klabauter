"""`sizing-assemble --write` records its session state when `.git` is write-denied.

A sandboxed child cannot write under `.git`. Falsifier: red while the per-session hub lives at
`<git-common-dir>/coordinator-sessions`, green once it lives under `<repo>/.coordinator-local`.
The denial is simulated by patching the create/open/mkdir/replace primitives; no ACL is touched.
"""
from __future__ import annotations

import errno
import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.sizing_assemble as sa
from coordinator_core.session_hub_path import hub_for_root
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "cccccccc-dddd-eeee-ffff-000000000009"
_REL = "state/sizings/2026-11-10-denied.yaml"
_WRITE_FLAGS = os.O_CREAT | os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_TRUNC
_FIVE = ("head_at_start", "meta.json", "started_at", "touch-record.jsonl", "xs-lane.json")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    )


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def _deny_git_writes(monkeypatch: pytest.MonkeyPatch, git: Path) -> list[str]:
    """Refuse every mkdir/open-for-write/replace under *git*; returns the denied paths."""
    git_s = os.path.normcase(os.path.realpath(str(git)))
    denied: list[str] = []

    def _under_git(path: object) -> bool:
        try:
            p = os.path.normcase(os.path.realpath(os.fsdecode(path)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return p == git_s or p.startswith(git_s + os.sep)

    def _refuse(path: object) -> PermissionError:
        denied.append(os.fsdecode(path))  # type: ignore[arg-type]
        return PermissionError(errno.EACCES, "Permission denied", os.fsdecode(path))  # type: ignore[arg-type]

    real_open, real_mkdir, real_replace = os.open, os.mkdir, os.replace

    def fake_open(path, flags, *args, **kwargs):
        if flags & _WRITE_FLAGS and _under_git(path):
            raise _refuse(path)
        return real_open(path, flags, *args, **kwargs)

    def fake_mkdir(path, *args, **kwargs):
        if _under_git(path):
            raise _refuse(path)
        return real_mkdir(path, *args, **kwargs)

    def fake_replace(src, dst, *args, **kwargs):
        if _under_git(dst):
            raise _refuse(dst)
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "mkdir", fake_mkdir)
    monkeypatch.setattr(os, "replace", fake_replace)
    return denied


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "seed.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    (tmp_path / _REL).write_text("schema: sizing-object\n", encoding="utf-8")
    for name in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sa, "write_back", lambda *a, **k: {"pm_verbatims": {"embedded": 0, "turns": None}}
    )
    return tmp_path


def test_sizing_write_records_session_state_outside_git(repo, monkeypatch):
    git = repo / ".git"
    before = _tree(git)
    denied = _deny_git_writes(monkeypatch, git)

    from coordinator_core.session import core

    # The session-start leg a child runs before sizing-assemble (head_at_start, meta.json,
    # started_at); sizing-assemble itself writes touch-record.jsonl and xs-lane.json.
    assert core.ensure_session(_SID, str(repo)) is not None
    rc = sa.main(["--tshirt", "XS", "--write", _REL])

    assert rc == sa.EXIT_OK
    assert denied == []
    hub = hub_for_root(str(repo))
    assert hub == repo / ".coordinator-local" / "coordinator-sessions"
    sdir = hub / _SID
    for name in _FIVE:
        assert (sdir / name).is_file(), name
    assert _tree(git) == before


def test_denial_simulation_refuses_the_legacy_hub(repo, monkeypatch):
    _deny_git_writes(monkeypatch, repo / ".git")
    with pytest.raises(PermissionError):
        os.mkdir(repo / ".git" / "coordinator-sessions")
