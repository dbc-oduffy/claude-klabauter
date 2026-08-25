"""Characterization tests for coordinator_core.ops.scope_soak_enable.

Ports the bash oracle 1:1, plus the not-in-git-repo path the bash suite
never exercised.

Port of: test-scope-soak-enable.sh (DoE 67202df6, 2026-07-16)
Spec backlink: DoE scratch/subagent-sandbox/bash-to-python-engine-migration/
    recipe-small-bin-clis-gate-aging-scope-soak-scope-warning.md § 2
"""
from __future__ import annotations

import os
import re

from coordinator_core.ops.scope_soak_enable import enable, main

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_ISO_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


def test_1_creates_sentinel_when_not_present(tmp_path):
    text, rc = enable(git_root=str(tmp_path))
    sentinel = tmp_path / ".git" / "coordinator-sessions" / ".warn-mode-enabled-at"
    assert sentinel.is_file()
    assert rc == 0


def test_2_sentinel_contains_valid_iso_timestamp(tmp_path):
    enable(git_root=str(tmp_path))
    sentinel = tmp_path / ".git" / "coordinator-sessions" / ".warn-mode-enabled-at"
    ts = sentinel.read_text(encoding="utf-8").strip()
    assert _ISO_UTC_RE.match(ts), f"sentinel timestamp format invalid: {ts!r}"


def test_3_idempotent_second_run_does_not_overwrite(tmp_path):
    enable(git_root=str(tmp_path))
    sentinel = tmp_path / ".git" / "coordinator-sessions" / ".warn-mode-enabled-at"
    first_ts = sentinel.read_text(encoding="utf-8").strip()

    text2, rc2 = enable(git_root=str(tmp_path))
    second_ts = sentinel.read_text(encoding="utf-8").strip()

    assert first_ts == second_ts
    assert "already" in text2.lower()
    assert rc2 == 0


def test_not_in_git_repo_exits_1(capsys):
    # An explicit falsy git_root (empty string) exercises the identical
    # `if not git_root` branch _resolve_git_root() returning None would hit —
    # deterministic without depending on the test-runner's cwd being outside
    # a repo (which is almost never true in this dev environment).
    text, rc = enable(git_root="")
    captured = capsys.readouterr()
    assert rc == 1
    assert text == ""
    assert "not inside a git repository" in captured.err


def test_main_wraps_enable_and_writes_stdout(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Soak clock started." in captured.out
