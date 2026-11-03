"""A git child started under a request deadline never outlives it; no deadline keeps today's behaviour."""
from __future__ import annotations

import subprocess
import sys
import time
from types import SimpleNamespace

from coordinator_core import dispatch_abandon
from coordinator_core.git_lock_retry import run_with_lock_retry
from coordinator_core.ops.ceremony import git_native


def test_child_killed_when_request_deadline_nearly_spent(monkeypatch, tmp_path):
    real_run = subprocess.run

    def hanging_run(argv, **kw):
        return real_run([sys.executable, "-c", "import time; time.sleep(30)"], **kw)

    monkeypatch.setattr(git_native.subprocess, "run", hanging_run)
    token = dispatch_abandon.DEADLINE.set(time.monotonic() + 0.2)
    try:
        start = time.monotonic()
        result = git_native._git(["status"], cwd=tmp_path, timeout=60)
        elapsed = time.monotonic() - start
    finally:
        dispatch_abandon.DEADLINE.reset(token)
    assert result.returncode == -1
    assert "timed out" in result.stderr
    assert elapsed < 1.0


def test_unbound_deadline_keeps_caller_timeout(monkeypatch, tmp_path):
    seen = []

    def fake_run(argv, **kw):
        seen.append(kw["timeout"])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(git_native.subprocess, "run", fake_run)
    assert dispatch_abandon.remaining() is None
    git_native._git(["status"], cwd=tmp_path, timeout=45)
    assert seen == [45]


def test_ample_deadline_does_not_shorten_timeout(monkeypatch, tmp_path):
    seen = []

    def fake_run(argv, **kw):
        seen.append(kw["timeout"])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(git_native.subprocess, "run", fake_run)
    token = dispatch_abandon.DEADLINE.set(time.monotonic() + 500)
    try:
        git_native._git(["status"], cwd=tmp_path, timeout=45)
    finally:
        dispatch_abandon.DEADLINE.reset(token)
    assert seen == [45]


def _locked():
    return SimpleNamespace(returncode=1, stderr="fatal: Unable to create index.lock")


def test_retry_starts_no_attempt_past_deadline():
    calls = []

    def invoke():
        calls.append(1)
        return _locked()

    token = dispatch_abandon.DEADLINE.set(time.monotonic() - 1)
    try:
        start = time.monotonic()
        result = run_with_lock_retry(invoke)
    finally:
        dispatch_abandon.DEADLINE.reset(token)
    assert len(calls) == 1
    assert result.returncode == 1
    assert time.monotonic() - start < 0.5


def test_retry_unbound_still_retries():
    calls = []

    def invoke():
        calls.append(1)
        return _locked()

    run_with_lock_retry(invoke, max_attempts=3, backoff_schedule_s=(0.0, 0.0))
    assert len(calls) == 3
