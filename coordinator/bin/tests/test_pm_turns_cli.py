"""pm-turns CLI: quote/referent read-back is byte-identical, absent turns exit 1, bad usage 2."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.spawns_process

REPO = Path(__file__).resolve().parents[3]
CLI = REPO / "coordinator" / "bin" / "pm-turns.py"
SID = "pmturns-test-sid"
TRICKY = 'say "hi"\nline two sep é中文\n'


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (tmp_path / ".git" / "coordinator-sessions" / SID).mkdir(parents=True)
    return tmp_path


def _append(repo, prompt, referent=None):
    sys.path.insert(0, str(REPO))
    from coordinator_core.ops.baton_pm_turns import append_turn

    res = append_turn(prompt, session_id=SID, cwd=str(repo), referent=referent)
    assert res["ok"], res
    return res


def _run(repo, *args):
    return subprocess.run([sys.executable, str(CLI), *args], cwd=repo,
                          capture_output=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _log_lines(repo):
    path = repo / ".git" / "coordinator-sessions" / SID / "pm_turns.jsonl"
    return path.read_bytes().split(b"\n")


def test_quote_is_byte_identical(repo):
    _append(repo, "first")
    _append(repo, TRICKY)
    proc = _run(repo, "quote", "1", "--session-id", SID)
    assert proc.returncode == 0, proc.stderr
    stored = json.loads(_log_lines(repo)[1].decode("utf-8"))["prompt"]
    assert proc.stdout == stored.encode("utf-8") == TRICKY.encode("utf-8")


def test_absent_turn_exits_1_with_empty_stdout(repo):
    _append(repo, "only")
    proc = _run(repo, "quote", "9", "--session-id", SID)
    assert proc.returncode == 1
    assert proc.stdout == b""
    assert proc.stderr


def test_non_int_exits_2(repo):
    assert _run(repo, "quote", "abc").returncode == 2
    assert _run(repo).returncode == 2


def test_referent_round_trips(repo):
    excerpt = 'EM said "go"\n é '
    _append(repo, "yes", referent={"excerpt": excerpt})
    proc = _run(repo, "referent", "0", "--session-id", SID)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == excerpt.encode("utf-8")


def test_referent_falls_back_to_transcript_uuid(repo):
    _append(repo, "yes", referent={"transcript_uuid": "abc-123"})
    proc = _run(repo, "referent", "0", "--session-id", SID)
    assert (proc.returncode, proc.stdout) == (0, b"abc-123")


def test_referent_absent_exits_1(repo):
    _append(repo, "no referent")
    proc = _run(repo, "referent", "0", "--session-id", SID)
    assert proc.returncode == 1
    assert proc.stdout == b""


def test_process_time_under_200ms(repo):
    resource = pytest.importorskip("resource")
    _append(repo, "x")
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    proc = _run(repo, "quote", "0", "--session-id", SID)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    assert proc.returncode == 0 and proc.stdout == b"x"
    spent = (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)
    assert spent < 0.2, spent
