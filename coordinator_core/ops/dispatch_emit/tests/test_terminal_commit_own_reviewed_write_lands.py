"""A path whose working-tree bytes are only the run's own executor write lands at the run's final
commit; a foreign rewrite after that write still refuses blob-drift.

The executor's Write/Edit reaches no session touch ledger, so the session's newest event for the path
is its own READ of the pre-run bytes, and that hash is the commit's expectation. The run's record dir
(``measured-rev.json``, ``kept.json``) holds the blob the executor left. ``dispatch.terminal_commit``
re-measures a refused path against that record; ``dispatch.land_run``, which an ask run's commit leg
calls, does not."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import (  # noqa: F401 -- repo is a fixture
    RUN,
    SELF,
    _declare,
    _git,
    _land,
    _record_dir,
    _write,
    repo,
)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_refuses_blob_drift import (
    SID,
    _fire,
    _sha,
)
from coordinator_core.session import touch_record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BASE = "def f():\n    return 0\n"
_OWN = "def f():\n    return 1\n"
_FOREIGN = "def f():\n    return 2\n"


def _blob(text: str) -> str:
    """The git blob sha of ``text`` in checkin (LF) form."""
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _git_out(args, cwd: Path) -> str:
    return _git(args, cwd).strip()


def _read_event(root: Path, session: str, rel: str, text: str, at: float | None = None) -> None:
    """The EM session's own READ of the pre-run bytes: the newest session event for the path."""
    sink = touch_record.sink_path(root / ".git" / "coordinator-sessions" / session)
    touch_record.append_event(
        sink, session_id=session, agent_id=None, verb="T", path=rel, content_hash=_sha(text), kind="r",
        **({} if at is None else {"timestamp": at}),
    )


def _record_blobs(record: Path, root: Path, rel: str, text: str) -> None:
    """``measured-rev.json`` and ``kept.json`` naming the executor's blob for ``rel``."""
    record.mkdir(parents=True, exist_ok=True)
    (record / "measured-rev.json").write_text(
        json.dumps({"base_sha": _git_out(["rev-parse", "HEAD"], root), "blobs": {rel: _blob(text)}}),
        encoding="utf-8",
    )
    (record / "kept.json").write_text(
        json.dumps({"chunks": {"C1": {"paths": {rel: _blob(text)}}}, "plan_gate": None}), encoding="utf-8"
    )


def _bytes(text: str, crlf: bool) -> bytes:
    return (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")


@pytest.fixture
def tc_repo(tmp_path):
    root = tmp_path / "tc-repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _stage_terminal(root: Path, text: str, *, crlf: bool) -> None:
    if crlf:
        _git(["config", "core.autocrlf", "true"], root)
    (root / "a.py").write_bytes(_BASE.encode("utf-8"))
    _git(["add", "a.py"], root)
    _git(["commit", "-q", "-m", "base"], root)
    _read_event(root, SID, "a.py", _BASE, 1000.0)
    (root / "a.py").write_bytes(_bytes(text, crlf))
    _record_blobs(root / RECORD_DIR_ROOT / "run-1", root, "a.py", _OWN)


def _stage_land(root: Path, text: str, *, crlf: bool) -> None:
    if crlf:
        _git(["config", "core.autocrlf", "true"], root)
    _write(root, "src/a.py", _BASE)
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "base"], root)
    _read_event(root, SELF, "src/a.py", _BASE)
    _declare(root, "src/a.py")
    _record_dir(root)
    (root / "src/a.py").write_bytes(_bytes(text, crlf))
    _record_blobs(root / RECORD_DIR_ROOT / RUN, root, "src/a.py", _OWN)


@pytest.mark.parametrize("crlf", [False, True], ids=["lf", "crlf"])
def test_terminal_commit_lands_the_runs_own_write_over_the_sessions_pre_run_read(tc_repo, monkeypatch, crlf):
    _stage_terminal(tc_repo, _OWN, crlf=crlf)
    out, seen = _fire(tc_repo, monkeypatch)
    assert out["committed"] is True, out
    assert seen[0]["expected_content_sha256"]["a.py"] == _sha(_BASE)


@pytest.mark.parametrize("crlf", [False, True], ids=["lf", "crlf"])
def test_terminal_commit_still_refuses_a_foreign_rewrite(tc_repo, monkeypatch, crlf):
    _stage_terminal(tc_repo, _FOREIGN, crlf=crlf)
    head = _git_out(["rev-parse", "HEAD"], tc_repo)
    out, _ = _fire(tc_repo, monkeypatch)
    assert out["refused"] == "blob-drift"
    assert [d["path"] for d in out["blob_drift"]] == ["a.py"]
    assert _git_out(["rev-parse", "HEAD"], tc_repo) == head


@pytest.mark.parametrize("crlf", [False, True], ids=["lf", "crlf"])
def test_land_run_lands_the_runs_own_write_over_the_sessions_pre_run_read(repo, crlf):
    _stage_land(repo, _OWN, crlf=crlf)
    out = _land(repo)
    assert out.get("refused") != "blob-drift", out
    assert out["committed"] is True, out
    assert "src/a.py" in _git(["show", "--name-only", "--format=", "HEAD"], repo).split()


@pytest.mark.parametrize("crlf", [False, True], ids=["lf", "crlf"])
def test_land_run_still_refuses_a_foreign_rewrite(repo, crlf):
    _stage_land(repo, _FOREIGN, crlf=crlf)
    head = _git_out(["rev-parse", "HEAD"], repo)
    out = _land(repo)
    assert out["committed"] is False and out["refused"] == "blob-drift", out
    assert [d["path"] for d in out["blob_drift"]] == ["src/a.py"]
    assert _git_out(["rev-parse", "HEAD"], repo) == head
