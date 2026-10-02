
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
    monkeypatch.setattr(rco, "_read_memo_surface", lambda mode, *, repo_root=None: ReaderResult())
    monkeypatch.setattr(rco, "_read_rag_staleness", lambda: ReaderResult())
    monkeypatch.setattr(rco, "_read_worktree_sweep", lambda *, repo_root=None: ReaderResult())

    for cadence in ("session", "day", "week"):
        rco.collect(cadence)

    assert not any("fetch" in call for call in forbid_git_fetch)


def test_branch_reconcile_collect_is_read_only(forbid_disk_mutation, forbid_git_fetch, monkeypatch):
    monkeypatch.setattr(rbr, "_read_span_assert", lambda repo_root=None: ReaderResult())
    monkeypatch.setattr(rbr, "_read_auto_reconcile", lambda repo_root=None: ReaderResult())

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


def test_health_reaper_collect_is_read_only(
    forbid_disk_mutation, forbid_git_fetch, monkeypatch
):
    monkeypatch.setattr(rhr, "_read_claude_klabauter_bin_sentinel", lambda: ReaderResult())
    monkeypatch.setattr(rhr, "_read_working_repo_registration", lambda: ReaderResult())
    monkeypatch.setattr(rhr, "_read_ceremony_hook", lambda cadence: ReaderResult())
    monkeypatch.setattr(rhr, "_read_marker_freshness", lambda cadence: ReaderResult())

    for cadence in ("session", "day", "week"):
        rhr.collect(cadence)

    assert not any("fetch" in call for call in forbid_git_fetch)


def test_working_repo_registration_reader_never_spawns_a_subprocess(forbid_git_fetch, monkeypatch):

    def _forbidden_run(*_args, **_kwargs):
        raise AssertionError("working-repo-registration reader must be zero-spawn")

    monkeypatch.setattr(subprocess, "run", _forbidden_run)
    rhr._read_working_repo_registration()
    assert not any("fetch" in call for call in forbid_git_fetch)
