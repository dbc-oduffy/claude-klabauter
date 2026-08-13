"""Tests for coordinator_core.ops.check_registry_codename_leak.

Port of: check-registry-codename-leak.sh (coordinator-claude b5a4192c, 2026-07-20)
"""
from __future__ import annotations

import os

import pytest

from coordinator_core.ops.check_registry_codename_leak import _resolve_registry_keys, main


def _env(**overrides):
    base = dict(os.environ)
    base.update(overrides)
    return base


def test_usage_error_no_args(capsys):
    rc = main([], env=_env())
    assert rc == 2
    captured = capsys.readouterr()
    assert "Usage: check-registry-codename-leak.sh <target-dir>" in captured.err


def test_usage_error_too_many_args(capsys):
    rc = main(["a", "b"], env=_env())
    assert rc == 2


def test_missing_target_dir(tmp_path, capsys):
    missing = tmp_path / "does-not-exist"
    rc = main([str(missing)], env=_env())
    assert rc == 2
    captured = capsys.readouterr()
    assert "target-dir not found" in captured.err


def test_positive_leak_underscore_and_hyphen_forms(tmp_path, capsys):
    d = tmp_path / "pos"
    d.mkdir()
    (d / "notes.md").write_text(
        "Internal reference to project_zolithane must never leak into the public tree.\n"
        "Also see project-zolithane in hyphen form.\n"
    )
    rc = main(
        [str(d)],
        env=_env(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.experiments repos.project_zolithane"),
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "private codename(s) found in publish tree" in captured.err
    assert "project_zolithane" in captured.err


def test_negative_clean_tree(tmp_path, capsys):
    d = tmp_path / "neg"
    d.mkdir()
    (d / "notes.md").write_text(
        "We ran a batch of experiments this week to validate the new pipeline.\n"
        "Nothing private here, just coordinator and example_retrieval_repo mentions.\n"
    )
    rc = main(
        [str(d)],
        env=_env(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.experiments repos.project_zolithane"),
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "clean" in captured.err


def test_keepset_experiments_not_flagged(tmp_path, capsys):
    d = tmp_path / "kept"
    d.mkdir()
    (d / "notes.md").write_text("See docs/experiments/2026-07-09-batch-results.md.\n")
    rc = main([str(d)], env=_env(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.experiments"))
    assert rc == 0


def test_keepset_prefix_match_coordinator_claude(tmp_path):
    d = tmp_path / "kept2"
    d.mkdir()
    (d / "notes.md").write_text("coordinator_claude is our system vocabulary.\n")
    rc = main([str(d)], env=_env(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.coordinator_claude"))
    assert rc == 0


def test_machine_local_absent_no_override_warns_and_exits_zero(tmp_path, capsys, monkeypatch):
    d = tmp_path / "pos"
    d.mkdir()
    (d / "notes.md").write_text("project_zolithane leak here.\n")
    env = _env()
    env.pop("COORDINATOR_CODENAME_REGISTRY_KEYS", None)
    env["PATH"] = "/usr/bin:/bin"
    env["HOME"] = "/nonexistent-home-for-test"
    rc = main([str(d)], env=env)
    assert rc == 0
    captured = capsys.readouterr()
    assert "WARNING: machine-local not found" in captured.err
    assert "no private codenames to check" in captured.err


def _write_fake_ml(path, keys_output: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nif [ \"$1\" = keys ]; then echo '{keys_output}'; fi\n")
    path.chmod(0o755)


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec-bit fixture")
def test_settings_home_rung_wins_over_legacy_home(tmp_path, monkeypatch):
    """The settings-home install (DR-072, canonical) must be tried before the
    legacy ~/.claude/bin rung, which is retired 2026-07-28 and kept only for
    machines that predate the move — see [[verify_ue_overrides]]/
    [[verify_dist_publish_repo_sync]] sibling resolvers for the same ordering.
    """
    settings_ml = tmp_path / "settings" / "bin" / "machine-local"
    _write_fake_ml(settings_ml, "repos.from_settings_home")
    legacy_ml = tmp_path / "home" / ".claude" / "bin" / "machine-local"
    _write_fake_ml(legacy_ml, "repos.from_legacy_home")

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    env = _env()
    env.pop("COORDINATOR_CODENAME_REGISTRY_KEYS", None)
    env["PATH"] = "/usr/bin:/bin"
    env["HOME"] = str(tmp_path / "home")

    keys = _resolve_registry_keys(env)
    assert keys == ["repos.from_settings_home"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec-bit fixture")
def test_legacy_home_rung_still_works_when_settings_home_absent(tmp_path, monkeypatch):
    """Back-compat: a machine that predates the settings-home move must keep
    resolving via the legacy ~/.claude/bin rung."""
    legacy_ml = tmp_path / "home" / ".claude" / "bin" / "machine-local"
    _write_fake_ml(legacy_ml, "repos.from_legacy_home")

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-settings-here"))
    env = _env()
    env.pop("COORDINATOR_CODENAME_REGISTRY_KEYS", None)
    env["PATH"] = "/usr/bin:/bin"
    env["HOME"] = str(tmp_path / "home")

    keys = _resolve_registry_keys(env)
    assert keys == ["repos.from_legacy_home"]


def test_excludes_git_and_backup_files(tmp_path):
    d = tmp_path / "excl"
    d.mkdir()
    (d / ".git").mkdir()
    (d / ".git" / "COMMIT_EDITMSG").write_text("mentions project_zolithane\n")
    (d / "scratch.bak").write_text("project_zolithane in a backup\n")
    (d / "clean.md").write_text("no leaks here\n")
    rc = main(
        [str(d)],
        env=_env(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.project_zolithane"),
    )
    assert rc == 0


@pytest.mark.skipif(os.name == "nt", reason="chmod-based unreadable-file fixture is POSIX-only")
def test_unreadable_file_fails_closed_even_if_no_leak_found(tmp_path, capsys):
    """BEHAVIOUR CHANGE regression (2026-07-22): this guard's own docstring
    declares fail-closed intent; an unreadable file must not let a clean-
    looking scan report success. Previously returned 0 here."""
    d = tmp_path / "incomplete"
    d.mkdir()
    (d / "clean.md").write_text("nothing sensitive here\n")
    blocked = d / "blocked.md"
    blocked.write_text("placeholder\n")
    os.chmod(blocked, 0o000)
    try:
        rc = main(
            [str(d)],
            env=_env(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.project_zolithane"),
        )
    finally:
        os.chmod(blocked, 0o644)
    captured = capsys.readouterr()
    assert rc == 1
    assert "scan incomplete" in captured.err
    assert "blocked.md" in captured.err


def _example_doctrine_repo_fixture(tmp_path):
    d = tmp_path / "doe-em"
    d.mkdir()
    (d / "notes.md").write_text(
        "This tree references coordinator-claude-em in a role-id context.\n"
    )
    return d


def test_no_exempt_absent_example_doctrine_repo_still_exempt_today(tmp_path, capsys):
    """AC2 pin: fixture tree containing coordinator-claude-em, NO re-admission ->
    exits 0. Regression guard on the global default (example_doctrine_repo stays kept
    unless a target explicitly re-admits it)."""
    d = _example_doctrine_repo_fixture(tmp_path)
    rc = main([str(d)], env=_env(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.example_doctrine_repo"))
    assert rc == 0
    captured = capsys.readouterr()
    assert "no private codenames to check" in captured.err


def test_no_exempt_flag_reveals_example_doctrine_repo_leak(tmp_path, capsys):
    """AC2: --no-exempt example_doctrine_repo re-admits the slug -> exit 1, hit report
    cites the file in path:lineno:line shape."""
    d = _example_doctrine_repo_fixture(tmp_path)
    rc = main(
        ["--no-exempt", "example_doctrine_repo", str(d)],
        env=_env(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.example_doctrine_repo"),
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "private codename(s) found in publish tree" in captured.err
    hit_line = f"{d / 'notes.md'}:1:"
    assert hit_line in captured.err


def test_no_exempt_env_var_reveals_example_doctrine_repo_leak(tmp_path, capsys):
    """AC2: COORDINATOR_CODENAME_NO_EXEMPT env channel re-admits the slug ->
    exit 1, hit report cites the file in path:lineno:line shape."""
    d = _example_doctrine_repo_fixture(tmp_path)
    rc = main(
        [str(d)],
        env=_env(
            COORDINATOR_CODENAME_REGISTRY_KEYS="repos.example_doctrine_repo",
            COORDINATOR_CODENAME_NO_EXEMPT="example_doctrine_repo",
        ),
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "private codename(s) found in publish tree" in captured.err
    hit_line = f"{d / 'notes.md'}:1:"
    assert hit_line in captured.err


def test_no_exempt_flag_and_env_var_union(tmp_path, capsys):
    """Both channels union — either alone is sufficient, and passing both is
    not an error (dict.fromkeys dedupes)."""
    d = _example_doctrine_repo_fixture(tmp_path)
    rc = main(
        ["--no-exempt", "example_doctrine_repo", str(d)],
        env=_env(
            COORDINATOR_CODENAME_REGISTRY_KEYS="repos.example_doctrine_repo",
            COORDINATOR_CODENAME_NO_EXEMPT="example_doctrine_repo",
        ),
    )
    assert rc == 1


def test_no_exempt_slug_not_in_keepset_raises_and_names_keepset(tmp_path, capsys):
    """AC2 shape correction (NOT a status-quo pin): re-admitting a slug that
    is not an exact KEEPSET member must raise loud, not silently no-op. A
    `coordinator-claude` (hyphen) authoring slip against the `example_doctrine_repo` (underscore)
    KEEPSET entry must not re-admit nothing and produce a green publish."""
    d = _example_doctrine_repo_fixture(tmp_path)
    rc = main(
        ["--no-exempt", "coordinator-claude", str(d)],
        env=_env(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.example_doctrine_repo"),
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert "not in KEEPSET" in captured.err
    assert "coordinator-claude" in captured.err
    # error message names the valid KEEPSET members
    assert "example_retrieval_repo" in captured.err
    assert "example_doctrine_repo" in captured.err
    assert "coordinator" in captured.err


def test_no_exempt_env_var_slug_not_in_keepset_raises(tmp_path, capsys):
    """Same shape correction via the env channel."""
    d = _example_doctrine_repo_fixture(tmp_path)
    rc = main(
        [str(d)],
        env=_env(
            COORDINATOR_CODENAME_REGISTRY_KEYS="repos.example_doctrine_repo",
            COORDINATOR_CODENAME_NO_EXEMPT="not_a_real_keepset_slug",
        ),
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert "not in KEEPSET" in captured.err
    assert "not_a_real_keepset_slug" in captured.err


def test_unknown_flag_exits_two(tmp_path, capsys):
    """Oracle parity: an unrecognized long-option exits 2 with usage."""
    d = tmp_path / "unk"
    d.mkdir()
    rc = main(["--bogus-flag", str(d)], env=_env())
    assert rc == 2
    captured = capsys.readouterr()
    assert "Usage: check-registry-codename-leak.sh <target-dir>" in captured.err


def test_no_exempt_flag_missing_value_exits_two(capsys):
    """--no-exempt with no following value is a usage error, not an index
    crash."""
    rc = main(["--no-exempt"], env=_env())
    assert rc == 2
    captured = capsys.readouterr()
    assert "Usage: check-registry-codename-leak.sh <target-dir>" in captured.err


def test_extra_positional_after_no_exempt_exits_two(tmp_path, capsys):
    """Oracle parity: extra positionals still exit 2 even alongside
    --no-exempt."""
    d = tmp_path / "extra"
    d.mkdir()
    rc = main(["--no-exempt", "example_doctrine_repo", str(d), "extra-positional"], env=_env())
    assert rc == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
