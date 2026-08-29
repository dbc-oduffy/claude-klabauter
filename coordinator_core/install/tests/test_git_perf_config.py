"""Tests for coordinator_core.install.git_perf_config.

Covers the module's stated contract: idempotence, never clobbering a
differing value, one report line per SETTINGS key always, dry_run writing
nothing, the index actually being extended (not just the config key set),
and core.fsmonitor never appearing.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.install import git_perf_config as gpc

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _no_window_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo, *args):
    return subprocess.run(
        ("git", *args),
        cwd=str(repo),
        capture_output=True,
        text=True,
        creationflags=_no_window_flags(),
    )


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "a.txt").write_text("hello", encoding="utf-8", newline="\n")
    (repo / "b.txt").write_text("world", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


def _config_get(repo, key):
    proc = _git(repo, "config", "--get", key)
    return proc.stdout.strip() or None


def test_idempotence(tmp_path):
    repo = _init_repo(tmp_path)
    if not gpc.filesystem_supports_untracked_cache(repo):
        pytest.skip("filesystem failed git's untracked-cache mtime probe")

    first = gpc.apply(repo)
    assert any(
        line.startswith("set     core.untrackedCache") for line in first
    ), first

    second = gpc.apply(repo)
    assert any(
        line.startswith("ok      core.untrackedCache") and "(already set)" in line
        for line in second
    ), second

    assert _config_get(repo, "core.untrackedCache") == "true"


def test_does_not_clobber_a_differing_value(tmp_path):
    repo = _init_repo(tmp_path)
    _git(repo, "config", "core.untrackedCache", "false")

    report = gpc.apply(repo)

    assert _config_get(repo, "core.untrackedCache") == "false"
    matching = [
        line
        for line in report
        if line.startswith("left") and "core.untrackedCache" in line
    ]
    assert matching, report
    assert "not overwritten" in matching[0]


def test_every_setting_produces_a_report_line_always(tmp_path):
    repo = _init_repo(tmp_path)

    fresh_report = gpc.apply(repo)
    assert len(fresh_report) == len(gpc.SETTINGS)

    if not gpc.filesystem_supports_untracked_cache(repo):
        pytest.skip("filesystem failed git's untracked-cache mtime probe")

    again_report = gpc.apply(repo)
    assert len(again_report) == len(gpc.SETTINGS)


def test_dry_run_writes_nothing(tmp_path):
    repo = _init_repo(tmp_path)

    report = gpc.apply(repo, dry_run=True)

    assert any("would" in line for line in report), report
    assert _config_get(repo, "core.untrackedCache") is None


def test_index_is_actually_extended_not_just_config_key(tmp_path):
    repo = _init_repo(tmp_path)
    if not gpc.filesystem_supports_untracked_cache(repo):
        pytest.skip("filesystem failed git's untracked-cache mtime probe")

    gpc.apply(repo)

    assert _config_get(repo, "core.untrackedCache") == "true"

    # Review: coordinator:code-reviewer -- `update-index --test-untracked-cache`
    # is git's filesystem-support probe; it returns 0 regardless of whether the
    # index was ever extended, so it would still pass with the `update-index
    # --untracked-cache` call in apply() deleted entirely. The genuine proof
    # that the cache is LIVE in the index (not merely configured) is the
    # `UNTR` extension header appearing in `.git/index`'s raw bytes.
    with open(repo / ".git" / "index", "rb") as handle:
        index_bytes = handle.read()
    assert b"UNTR" in index_bytes, "index has no untracked-cache (UNTR) extension"

    second_report = gpc.apply(repo)
    assert any(
        line.startswith("ok      core.untrackedCache") for line in second_report
    ), second_report


def test_fsmonitor_never_set(tmp_path):
    repo = _init_repo(tmp_path)

    assert "core.fsmonitor" not in gpc.SETTINGS

    gpc.apply(repo)

    assert _config_get(repo, "core.fsmonitor") is None
