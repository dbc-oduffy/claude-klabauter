"""Tests for coordinator_core.install.git_perf_config.

Covers the module's stated contract: idempotence, never clobbering a
differing value, one report line per SETTINGS key always, dry_run writing
nothing, the index actually being extended (not just the config key set),
and core.fsmonitor never appearing.
"""

from __future__ import annotations

import pytest

from coordinator_core.install import git_perf_config as gpc
from coordinator_core.install.git_perf_config import _git

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


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

    # core.untrackedCache plus the three maintenance keys -- a caller that
    # prints nothing on a no-op cannot tell "already correct" from "never ran",
    # so every key reports on every call.
    expected = 1 + len(gpc._MAINTENANCE_KEYS)

    fresh_report = gpc.apply(repo)
    assert len(fresh_report) == expected, fresh_report

    if not gpc.filesystem_supports_untracked_cache(repo):
        pytest.skip("filesystem failed git's untracked-cache mtime probe")

    again_report = gpc.apply(repo)
    assert len(again_report) == expected, again_report


def test_three_maintenance_keys_land_in_the_repo(tmp_path):
    repo = _init_repo(tmp_path)

    gpc.apply(repo)

    assert _config_get(repo, "maintenance.strategy") == "incremental"
    assert _config_get(repo, "maintenance.auto") == "false"
    # NOT in the originating ask -- required because git's schedules cascade,
    # so prefetch runs at daily and weekly too and puts both over the 500ms bar.
    assert _config_get(repo, "maintenance.prefetch.enabled") == "false"


def test_maintenance_register_is_never_invoked(tmp_path, monkeypatch):
    """`git maintenance register` writes this repo's path into the operator's
    GLOBAL config. The design never runs the scheduler that reads it."""
    repo = _init_repo(tmp_path)
    invoked = []
    real_git = gpc._git

    def recording_git(r, *args, **kwargs):
        invoked.append(args)
        return real_git(r, *args, **kwargs)

    monkeypatch.setattr(gpc, "_git", recording_git)
    gpc.apply(repo)

    assert not any("register" in a for args in invoked for a in args), invoked
    assert not any(args[:1] == ("maintenance",) for args in invoked), invoked


def test_a_differing_maintenance_value_is_reported_not_overwritten(tmp_path):
    """The module's standing negative spec: a peer machine may differ
    deliberately, and this writer never wins that argument."""
    repo = _init_repo(tmp_path)
    _git(repo, "config", "maintenance.strategy", "none")

    report = gpc.apply(repo)

    assert _config_get(repo, "maintenance.strategy") == "none"
    assert any("left" in line and "maintenance.strategy" in line for line in report), report


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

    gpc.apply(repo)

    assert _config_get(repo, "core.fsmonitor") is None


def _fake_registry_helpers(roots, classify):
    def registry_repo_roots(_bin_dir):
        return list(roots)

    def classify_target(root):
        return classify(root)

    return registry_repo_roots, classify_target


def test_apply_fleet_applies_to_multiple_repos_and_is_idempotent(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    repo_a = _init_repo(tmp_path / "a")
    repo_b = _init_repo(tmp_path / "b")
    if not gpc.filesystem_supports_untracked_cache(repo_a):
        pytest.skip("filesystem failed git's untracked-cache mtime probe")

    roots = [("repos.a", str(repo_a)), ("repos.b", str(repo_b))]
    monkeypatch.setattr(
        gpc,
        "_git_hook_install_registry_helpers",
        lambda: _fake_registry_helpers(roots, lambda root: "worktree"),
    )

    first = gpc.apply_fleet(tmp_path)
    assert any("repos.a: set     core.untrackedCache" in line for line in first), first
    assert any("repos.b: set     core.untrackedCache" in line for line in first), first
    assert any(line.startswith("fleet summary:") for line in first), first
    assert _config_get(repo_a, "core.untrackedCache") == "true"
    assert _config_get(repo_b, "core.untrackedCache") == "true"

    second = gpc.apply_fleet(tmp_path)
    assert any("repos.a: ok      core.untrackedCache" in line for line in second), second
    assert any("repos.b: ok      core.untrackedCache" in line for line in second), second


def test_apply_fleet_reports_missing_without_raising(tmp_path, monkeypatch):
    missing_path = str(tmp_path / "does-not-exist")
    roots = [("repos.gone", missing_path)]
    monkeypatch.setattr(
        gpc,
        "_git_hook_install_registry_helpers",
        lambda: _fake_registry_helpers(roots, lambda root: "missing"),
    )

    report = gpc.apply_fleet(tmp_path)

    assert any("missing" in line and missing_path in line for line in report), report
    assert any(line.startswith("fleet summary:") for line in report), report


def test_apply_fleet_skips_mirrors_silently(tmp_path, monkeypatch):
    (tmp_path / "mirror").mkdir()
    repo_mirror = _init_repo(tmp_path / "mirror")
    roots = [("repos.mirror", str(repo_mirror))]
    monkeypatch.setattr(
        gpc,
        "_git_hook_install_registry_helpers",
        lambda: _fake_registry_helpers(roots, lambda root: "mirror"),
    )

    report = gpc.apply_fleet(tmp_path)

    assert not any("core.untrackedCache" in line for line in report), report
    assert any(line.startswith("fleet summary:") for line in report), report


def test_apply_fleet_empty_fleet_is_explicit_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        gpc,
        "_git_hook_install_registry_helpers",
        lambda: _fake_registry_helpers([], lambda root: "worktree"),
    )

    report = gpc.apply_fleet(tmp_path)

    assert len(report) == 1
    assert "found no registered repos" in report[0]
    assert "not the same fact as 'every repo is current'" in report[0]


def test_apply_fleet_one_repo_raising_does_not_discard_the_others(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "c").mkdir()
    repo_a = _init_repo(tmp_path / "a")
    repo_c = _init_repo(tmp_path / "c")
    if not gpc.filesystem_supports_untracked_cache(repo_a):
        pytest.skip("filesystem failed git's untracked-cache mtime probe")

    roots = [
        ("repos.a", str(repo_a)),
        ("repos.b", "unreachable-mid-fleet"),
        ("repos.c", str(repo_c)),
    ]

    def classify(root):
        if root == "unreachable-mid-fleet":
            raise FileNotFoundError("git not found on PATH")
        return "worktree"

    monkeypatch.setattr(
        gpc,
        "_git_hook_install_registry_helpers",
        lambda: _fake_registry_helpers(roots, classify),
    )

    report = gpc.apply_fleet(tmp_path)

    assert any("repos.a: set     core.untrackedCache" in line for line in report), report
    assert any("repos.c: set     core.untrackedCache" in line for line in report), report
    assert any(line.startswith("FAILED  repos.b:") for line in report), report
    assert any(line.startswith("fleet summary:") for line in report), report


def test_apply_fleet_helper_import_unavailable_degrades_to_advisory(tmp_path, monkeypatch):
    monkeypatch.setattr(gpc, "_git_hook_install_registry_helpers", lambda: None)

    report = gpc.apply_fleet(tmp_path)

    assert len(report) == 1
    assert "advisory" in report[0]
