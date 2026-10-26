"""land_run's landing set is closed: a dirty file no row declares never enters it, so a foreign commit's
lines that file drops cannot trip since-base-overlap. The same drop on a declared path still refuses."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c4"
RUN = "plan-20261021T100000-c4c4c4"
SCRIPT = "run-c4.mjs"
FOREIGN = "tests/test_foreign_shape.py"
REVIEWED = reviewed()


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _record(root: Path, rel: str) -> None:
    sink = touch_record.sink_path(root / ".git" / "coordinator-sessions" / SELF)
    touch_record.append_event(
        sink, session_id=SELF, agent_id=None, verb="T", path=rel,
        content_hash=touch_record.compute_content_hash(root / rel), kind="w",
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root, FOREIGN, "a = 1\n")
    _write(root, "docs/own.md", "own\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _foreign_drops_lines_after(repo: Path, rel: str) -> tuple:
    """Return (run base, bytes left on disk): a foreign commit adds a line after the base, then an
    uncommitted edit drops it."""
    base = _git(["rev-parse", "HEAD"], repo).strip()
    original = (repo / rel).read_text(encoding="utf-8")
    _write(repo, rel, original + "foreign_added = 2\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "foreign commit adds a line"], repo)
    _write(repo, rel, original)
    return base, original


def _land(repo: Path, base: str, declared: str) -> dict:
    _write(repo, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"run_id": RUN}))
    request = CommitRequest(chunks=(ChunkCommit(id="X1", title="t", paths=(declared,)),), plan_path=None)
    _write(repo, SCRIPT, "// emitted\n" + render_marker(request) + "\n")
    return land_run._handler(
        {"script_path": SCRIPT, "incomplete_chunks": [], "inline_review": dict(REVIEWED),
         "session_id": SELF, "run_id": RUN, "run_base_sha": base},
        repo_root=repo / ".git",
    )


def test_foreign_dirty_file_dropping_foreign_lines_never_enters_the_landing_set(repo):
    base, original = _foreign_drops_lines_after(repo, FOREIGN)
    _write(repo, "docs/own.md", "own\nedited\n")
    session_scope.touch(SELF, "docs/own.md", cwd=str(repo))
    _record(repo, "docs/own.md")
    session_scope.touch(SELF, FOREIGN, cwd=str(repo))
    _record(repo, FOREIGN)

    out = _land(repo, base, "docs/own.md")

    assert out.get("committed") is True, out
    assert out.get("refused") is None
    changed = sorted(_git(["show", "--name-only", "--format=", "HEAD"], repo).split())
    assert FOREIGN not in changed
    assert "docs/own.md" in changed
    assert (repo / FOREIGN).read_text(encoding="utf-8") == original
    assert FOREIGN in {ln[3:] for ln in _git(["status", "--porcelain"], repo).splitlines()}


def test_control_the_same_drop_on_a_declared_path_still_refuses(repo):
    base, _original = _foreign_drops_lines_after(repo, FOREIGN)
    session_scope.touch(SELF, FOREIGN, cwd=str(repo))
    _record(repo, FOREIGN)

    out = _land(repo, base, FOREIGN)

    assert out.get("refused") == "since-base-overlap", out
