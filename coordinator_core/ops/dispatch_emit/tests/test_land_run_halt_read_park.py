"""A halted dispatch.land_run whose file read stalls parks retry-safe; a re-send reconciles."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.bounded_io import IoIndeterminate
from coordinator_core.ops.dispatch_emit import land_run, land_run_halt, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-bbbbbb"
LEDGER = {"halted": "ledger-check", "reason": "row R1 open"}
STALLED = "state/warp/stalled/run.json"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _write(tmp_path, "docs/plans/peer.md", "peer\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    _write(tmp_path, f"{RECORD_DIR_ROOT}/{RUN}/run.json", "run\n")
    _write(tmp_path, "run.mjs", "// script\n")
    return tmp_path


def _params(token: str) -> dict:
    return {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: dict(LEDGER), "run_token": token}


def _op(root: Path, token: str) -> dict:
    """The registered dispatch.land_run handler at default params."""
    return land_run._handler(_params(token), repo_root=root / ".git")


def _stall_land(monkeypatch, *, after_commit: bool) -> list:
    """Make the halt landing's body raise IoIndeterminate, once the commit ran when `after_commit`."""
    real = land_run_halt._land
    calls: list = []

    def stalled(*args, **kwargs):
        calls.append(1)
        if after_commit:
            real(*args, **kwargs)
        raise IoIndeterminate(STALLED, 260)

    monkeypatch.setattr(land_run_halt, "_land", stalled)
    return calls


def test_stalled_read_before_any_commit_parks_retry_safe_and_a_resend_lands(repo, monkeypatch):
    token = run_token.mint(repo, RUN, "run.mjs")
    head = _git(repo, "rev-parse", "HEAD")
    with monkeypatch.context() as patched:
        _stall_land(patched, after_commit=False)
        out = _op(repo, token)

    assert out["committed"] is False
    assert out["parked"] is True and out["status"] == "not-started"
    assert out["refused"] == "halt-read-indeterminate" and out["retry_safe"] is True
    assert out["stalled_path"] == STALLED and out["stalled_ms"] == 260
    assert out["next_action"] == "re-send the same dispatch.land_run"
    assert _git(repo, "rev-parse", "HEAD") == head
    assert run_token.admits(repo, RUN, token, "run.mjs")

    again = _op(repo, token)
    assert again.get("committed") is True or again.get("already_landed") is True, again


def test_stall_after_the_commit_moved_a_ref_never_answers_not_started(repo, monkeypatch):
    token = run_token.mint(repo, RUN, "run.mjs")
    head = _git(repo, "rev-parse", "HEAD")
    with monkeypatch.context() as patched:
        _stall_land(patched, after_commit=True)
        out = _op(repo, token)

    assert _git(repo, "rev-parse", "HEAD") != head
    assert out.get("status") != "not-started" and out.get("parked") is not True
    assert out.get("committed") is True or (out["committed"] is False and out["retry_safe"] is True)

    again = _op(repo, token)
    assert again.get("committed") is True or again.get("already_landed") is True, again


def test_a_275ms_read_lands_the_halt_commit_instead_of_parking(repo, monkeypatch):
    from coordinator_core import bounded_io

    token = run_token.mint(repo, RUN, "run.mjs")
    real = bounded_io._read_registered
    slow: list = []

    def slow_read(path):
        if path.endswith((".json", ".mjs")):
            slow.append(path)
            time.sleep(0.275)
        return real(path)

    with monkeypatch.context() as patched:
        patched.setattr(bounded_io, "_read_registered", slow_read)
        out = _op(repo, token)

    assert slow, "no halt read went through the bounded reader"
    assert out["committed"] is True and not out.get("parked")
    assert out.get("refused") != "halt-read-indeterminate"


def test_a_read_past_the_500ms_bar_still_parks(repo, monkeypatch):
    from coordinator_core import bounded_io
    from coordinator_core.git import call_io_scope

    assert 0.275 < call_io_scope.HALT_READ_DEADLINE_SECS <= 0.5
    token = run_token.mint(repo, RUN, "run.mjs")
    real = bounded_io._read_registered

    def wedged(path):
        if path.endswith((".json", ".mjs")):
            time.sleep(0.8)
        return real(path)

    with monkeypatch.context() as patched:
        patched.setattr(bounded_io, "_read_registered", wedged)
        out = _op(repo, token)

    assert out["committed"] is False and out["parked"] is True
    assert out["refused"] == "halt-read-indeterminate" and out["retry_safe"] is True


def test_unstalled_halted_land_is_unchanged(repo):
    token = run_token.mint(repo, RUN, "run.mjs")
    out = _op(repo, token)
    assert out["committed"] is True and not out.get("parked")
    assert "retry_safe" not in out
