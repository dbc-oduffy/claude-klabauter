"""
coordinator_core.orient_assemble.tests.test_read_only_guarantee — C3
AC(c): every reader family performs ZERO disk mutation and ZERO `git
fetch` while computing `collect(cadence)`.

Strategy: monkeypatch every disk-write primitive (`Path.write_text`,
`Path.write_bytes`, `os.remove`, `os.replace`, `os.unlink`) to raise
`AssertionError` if invoked, and wrap `subprocess.run` to record every
invocation and reject any command whose argv contains the literal token
`"fetch"`. Underlying reads are monkeypatched to deterministic, fast
stand-ins (this file is not a live-environment smoke test — it is a
hermetic guarantee check) so the guard actually exercises each reader's
own code path rather than timing out against real environment state.

Spec backlink: DoE-claude:pln-computed-skills-b2-ceremony-st-e82420, chunk C3
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.orient_assemble import (
    readers_branch_reconcile as rbr,
    readers_clean_ops as rco,
    readers_handoff_triage as rht,
    readers_health_reaper as rhr,
)
from coordinator_core.orient_assemble.readers_clean_ops import ReaderResult


@pytest.fixture
def forbid_disk_mutation(monkeypatch):
    """Fail the test loudly if any write/delete primitive is invoked."""

    def _forbidden(name):
        def _raise(*args, **kwargs):
            raise AssertionError(f"disk mutation attempted via {name}(args={args!r})")

        return _raise

    monkeypatch.setattr(Path, "write_text", _forbidden("Path.write_text"))
    monkeypatch.setattr(Path, "write_bytes", _forbidden("Path.write_bytes"))
    monkeypatch.setattr(os, "remove", _forbidden("os.remove"))
    monkeypatch.setattr(os, "replace", _forbidden("os.replace"))
    monkeypatch.setattr(os, "unlink", _forbidden("os.unlink"))


@pytest.fixture
def forbid_git_fetch(monkeypatch):
    """Wrap `subprocess.run` to reject any argv containing the literal 'fetch'."""
    real_run = subprocess.run
    calls: list[list[str]] = []

    def _guarded_run(cmd, *args, **kwargs):
        argv = list(cmd) if not isinstance(cmd, str) else [cmd]
        calls.append(argv)
        assert "fetch" not in argv, f"git fetch attempted: {argv!r}"
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _guarded_run)
    return calls


def test_clean_ops_collect_is_read_only(forbid_disk_mutation, forbid_git_fetch, monkeypatch):
    monkeypatch.setattr(rco, "_read_em_environment", lambda: ReaderResult())
    monkeypatch.setattr(rco, "_scan_addon_health_run", lambda mode: ([], 0))
    monkeypatch.setattr(rco, "_read_memo_surface", lambda mode: ReaderResult())
    monkeypatch.setattr(rco, "_read_rag_staleness", lambda: ReaderResult())
    monkeypatch.setattr(rco, "_read_worktree_sweep", lambda: ReaderResult())

    for cadence in ("session", "day", "week"):
        rco.collect(cadence)

    assert not any("fetch" in call for call in forbid_git_fetch)


def test_branch_reconcile_collect_is_read_only(forbid_disk_mutation, forbid_git_fetch, monkeypatch):
    monkeypatch.setattr(rbr, "_read_span_assert", lambda: ReaderResult())
    monkeypatch.setattr(rbr, "_read_auto_reconcile", lambda: ReaderResult())

    for cadence in ("session", "day", "week"):
        rbr.collect(cadence)

    assert not any("fetch" in call for call in forbid_git_fetch)


def test_handoff_triage_collect_is_read_only(forbid_disk_mutation, forbid_git_fetch, monkeypatch):
    monkeypatch.setattr(rht, "_read_stale_plans", lambda: ReaderResult())
    monkeypatch.setattr(rht, "_read_ready", lambda: ReaderResult())
    monkeypatch.setattr(rht, "_read_awaiting_gate", lambda: ReaderResult())
    monkeypatch.setattr(rht, "_read_orphaned_plans", lambda: ReaderResult())

    for cadence in ("session", "day", "week"):
        rht.collect(cadence)

    assert not any("fetch" in call for call in forbid_git_fetch)


def test_health_reaper_collect_is_read_only_including_the_accepted_dry_run_subprocess(
    forbid_disk_mutation, forbid_git_fetch, monkeypatch
):
    monkeypatch.setattr(rhr, "_read_claude_klabauter_bin_sentinel", lambda: ReaderResult())
    monkeypatch.setattr(rhr, "_read_working_repo_registration", lambda: ReaderResult())
    monkeypatch.setattr(rhr, "_read_ceremony_hook", lambda cadence: ReaderResult())
    monkeypatch.setattr(rhr, "_read_marker_freshness", lambda cadence: ReaderResult())
    # The one accepted subprocess (reap-orphaned-in-flight-handoffs.py --dry-run)
    # must never be a `fetch`, and must not itself write to disk — replaced
    # with a fixture that proves the reader only interprets stdout, not a
    # real subprocess.run passthrough (avoids a slow real dry-run per test run).
    monkeypatch.setattr(rhr, "_read_reaper_dry_run", lambda: ReaderResult())

    for cadence in ("session", "day", "week"):
        rhr.collect(cadence)

    assert not any("fetch" in call for call in forbid_git_fetch)


def test_reaper_dry_run_subprocess_call_never_carries_fetch(monkeypatch, forbid_git_fetch):
    """`_read_reaper_dry_run` is the SOLE accepted subprocess in the whole
    assembler (per readers_health_reaper's module docstring); assert its
    real subprocess.run invocation is inert (mocked returncode/stdout) and
    still never carries `fetch` in argv."""

    class _FakeCompleted:
        returncode = 0
        stdout = "0 orphaned in_flight handoffs would be released (dry-run)\n"
        stderr = ""

    def _fake_run(cmd, *args, **kwargs):
        argv = list(cmd)
        assert "fetch" not in argv
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = rhr._read_reaper_dry_run()
    assert result.directives == []


def test_working_repo_registration_reader_never_spawns_a_subprocess(forbid_git_fetch, monkeypatch):
    """Zero-spawn contract (spec backlink: `workday-start-health-probes.py`
    `working-repo-registration` subcommand docstring) -- this reader must
    never be the assembler's second accepted subprocess; assert any
    subprocess.run call anywhere in the call path raises."""

    def _forbidden_run(*_args, **_kwargs):
        raise AssertionError("working-repo-registration reader must be zero-spawn")

    monkeypatch.setattr(subprocess, "run", _forbidden_run)
    rhr._read_working_repo_registration()
    assert not any("fetch" in call for call in forbid_git_fetch)
