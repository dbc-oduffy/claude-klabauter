from __future__ import annotations

import os

import pytest

from coordinator_core.ops.check_registry_codename_leak import _is_kept, _resolve_registry_keys, main


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


def test_keepset_fleet_root_not_flagged(tmp_path, capsys):
    """repos.fleet_root names the fleet's container dir, not a private repo.

    Regression for the claude-klabauter-coordinator-bin post_rsync
    no-residual-pattern failure on 'fleet_root' in
    lib/git_hook_install.py's _CONTAINER_REGISTRY_KEYS constant.
    """
    d = tmp_path / "kept-fleet-root"
    d.mkdir()
    (d / "git_hook_install.py").write_text(
        '_CONTAINER_REGISTRY_KEYS = frozenset({"repos.fleet_root"})\n'
    )
    rc = main([str(d)], env=_env(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.fleet_root"))
    assert rc == 0


def test_is_kept_fleet_root():
    assert _is_kept("fleet_root") is True


def test_keepset_prefix_match_coordinator_claude(tmp_path):
    d = tmp_path / "kept2"
    d.mkdir()
    (d / "notes.md").write_text("coordinator_claude is our system vocabulary.\n")
    rc = main([str(d)], env=_env(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.coordinator_claude"))
    assert rc == 0


def test_machine_local_absent_no_override_warns_and_exits_zero(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.ops.check_registry_codename_leak._merged_flat_registry",
        lambda: {},
    )
    d = tmp_path / "pos"
    d.mkdir()
    (d / "notes.md").write_text("project_zolithane leak here.\n")
    env = _env()
    env.pop("COORDINATOR_CODENAME_REGISTRY_KEYS", None)
    rc = main([str(d)], env=env)
    assert rc == 0
    captured = capsys.readouterr()
    assert "no private codenames to check" in captured.err


def _write_registry_toml(path, *, repos_key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[repos]\n{repos_key} = "x"\n')


def test_settings_home_rung_wins_over_legacy_home(tmp_path, monkeypatch):
    """``merged_flat_registry()`` resolves the registry directory via
    ``coordinator_core._settings_home.machine_local_dir()`` — the
    settings-home ladder only (``COORDINATOR_SETTINGS_HOME`` override, else
    ``CLAUDE_HOME``/home-derived ``.coordinator-claude-settings``). A legacy
    ``~/.claude/machine-local/registry.toml`` sitting alongside it is never
    consulted -- see ``check_machine_local_regeneratability.py``'s
    ``_resolve_registry_dir`` docstring: reading that legacy path caused every
    coordinator-owned key to false-WARN as unclassified on settings-home-
    native machines, so this legacy rung was removed, not merely reordered.
    """
    settings_registry = tmp_path / "settings" / "machine-local" / "registry.toml"
    _write_registry_toml(settings_registry, repos_key="from_settings_home")
    legacy_registry = tmp_path / "home" / ".claude" / "machine-local" / "registry.toml"
    _write_registry_toml(legacy_registry, repos_key="from_legacy_home")

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    env = _env()
    env.pop("COORDINATOR_CODENAME_REGISTRY_KEYS", None)

    keys = _resolve_registry_keys(env)
    assert keys == ["repos.from_settings_home"]


def test_legacy_home_rung_is_not_consulted_when_settings_home_empty(tmp_path, monkeypatch):
    """The legacy ``~/.claude/machine-local`` rung is retired, not a fallback:
    when ``COORDINATOR_SETTINGS_HOME`` resolves to a directory with no
    registry file, resolution reports an empty registry rather than falling
    back to a legacy registry that happens to exist. Pins the same
    intentional removal ``test_settings_home_rung_wins_over_legacy_home``
    pins, from the other direction (settings-home present-but-empty rather
    than settings-home populated)."""
    legacy_registry = tmp_path / "home" / ".claude" / "machine-local" / "registry.toml"
    _write_registry_toml(legacy_registry, repos_key="from_legacy_home")

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-settings-here"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    env = _env()
    env.pop("COORDINATOR_CODENAME_REGISTRY_KEYS", None)

    keys = _resolve_registry_keys(env)
    assert keys == []


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
    if os.geteuid() == 0:
        pytest.skip("permission bits do not block reads for root (see "
                     "baton_assemble/tests/test_adopt_prior_attempt_unreadable_candidate.py "
                     "for the same skip)")
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


def _doe_claude_fixture(tmp_path):
    d = tmp_path / "doe-em"
    d.mkdir()
    (d / "notes.md").write_text(
        "This tree references doe-claude-em in a role-id context.\n"
    )
    return d


def test_no_exempt_absent_doe_claude_still_exempt_today(tmp_path, capsys):
    d = _doe_claude_fixture(tmp_path)
    rc = main([str(d)], env=_env(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.doe_claude"))
    assert rc == 0
    captured = capsys.readouterr()
    assert "no private codenames to check" in captured.err


def test_no_exempt_flag_reveals_doe_claude_leak(tmp_path, capsys):
    d = _doe_claude_fixture(tmp_path)
    rc = main(
        ["--no-exempt", "doe_claude", str(d)],
        env=_env(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.doe_claude"),
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "private codename(s) found in publish tree" in captured.err
    hit_line = f"{d / 'notes.md'}:1:"
    assert hit_line in captured.err


def _example_doctrine_repo_fixture(tmp_path):
    d = tmp_path / "doe-alias"
    d.mkdir()
    (d / "notes.md").write_text(
        "This tree cites repos.example_doctrine_repo in a docstring.\n"
    )
    return d


def test_example_doctrine_repo_stays_exempt_default(tmp_path, capsys):
    d = _example_doctrine_repo_fixture(tmp_path)
    rc = main(
        [str(d)],
        env=_env(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.example_doctrine_repo"),
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "no private codenames to check" in captured.err


def test_no_exempt_flag_reveals_example_doctrine_repo_leak(tmp_path, capsys):
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


def test_no_exempt_env_var_reveals_doe_claude_leak(tmp_path, capsys):
    """AC2: COORDINATOR_CODENAME_NO_EXEMPT env channel re-admits the slug ->
    exit 1, hit report cites the file in path:lineno:line shape."""
    d = _doe_claude_fixture(tmp_path)
    rc = main(
        [str(d)],
        env=_env(
            COORDINATOR_CODENAME_REGISTRY_KEYS="repos.doe_claude",
            COORDINATOR_CODENAME_NO_EXEMPT="doe_claude",
        ),
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "private codename(s) found in publish tree" in captured.err
    hit_line = f"{d / 'notes.md'}:1:"
    assert hit_line in captured.err


def test_no_exempt_flag_and_env_var_union(tmp_path, capsys):
    d = _doe_claude_fixture(tmp_path)
    rc = main(
        ["--no-exempt", "doe_claude", str(d)],
        env=_env(
            COORDINATOR_CODENAME_REGISTRY_KEYS="repos.doe_claude",
            COORDINATOR_CODENAME_NO_EXEMPT="doe_claude",
        ),
    )
    assert rc == 1


def test_no_exempt_slug_not_in_keepset_raises_and_names_keepset(tmp_path, capsys):
    d = _doe_claude_fixture(tmp_path)
    rc = main(
        ["--no-exempt", "doe-claude", str(d)],
        env=_env(COORDINATOR_CODENAME_REGISTRY_KEYS="repos.doe_claude"),
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert "not in KEEPSET" in captured.err
    assert "doe-claude" in captured.err
    assert "project_rag" in captured.err
    assert "doe_claude" in captured.err
    assert "coordinator" in captured.err


def test_no_exempt_env_var_slug_not_in_keepset_raises(tmp_path, capsys):
    d = _doe_claude_fixture(tmp_path)
    rc = main(
        [str(d)],
        env=_env(
            COORDINATOR_CODENAME_REGISTRY_KEYS="repos.doe_claude",
            COORDINATOR_CODENAME_NO_EXEMPT="not_a_real_keepset_slug",
        ),
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert "not in KEEPSET" in captured.err
    assert "not_a_real_keepset_slug" in captured.err


def test_unknown_flag_exits_two(tmp_path, capsys):
    d = tmp_path / "unk"
    d.mkdir()
    rc = main(["--bogus-flag", str(d)], env=_env())
    assert rc == 2
    captured = capsys.readouterr()
    assert "Usage: check-registry-codename-leak.sh <target-dir>" in captured.err


def test_no_exempt_flag_missing_value_exits_two(capsys):
    rc = main(["--no-exempt"], env=_env())
    assert rc == 2
    captured = capsys.readouterr()
    assert "Usage: check-registry-codename-leak.sh <target-dir>" in captured.err


def test_extra_positional_after_no_exempt_exits_two(tmp_path, capsys):
    d = tmp_path / "extra"
    d.mkdir()
    rc = main(["--no-exempt", "doe_claude", str(d), "extra-positional"], env=_env())
    assert rc == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
