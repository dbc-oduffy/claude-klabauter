"""Tests for coordinator_core.ops.configure_git.

Port-parity coverage for coordinator/bin/coordinator-configure-git (DOE-PORT
bin-entrypoint variant).
"""
from __future__ import annotations

import subprocess

from coordinator_core.install.write_surface import StaticClause
from coordinator_core.ops import configure_git as cg


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, encoding="utf-8", check=True
    )


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def test_not_a_git_repo_fails(tmp_path, monkeypatch):
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    monkeypatch.chdir(empty)
    rc = cg.main([])
    assert rc == 1


def test_configures_fresh_repo(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = cg.main([])
    assert rc == 0
    assert _git(repo, "config", "--get", "gc.autoDetach").stdout.strip() == "false"
    assert _git(repo, "config", "--get", "core.checkStat").stdout.strip() == "minimal"


def test_idempotent_rerun_reports_no_change(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)
    assert cg.main([]) == 0
    capsys.readouterr()

    rc = cg.main([])
    assert rc == 0
    err = capsys.readouterr().err
    assert "already hardened (no change)" in err
    assert _git(repo, "config", "--get", "gc.autoDetach").stdout.strip() == "false"
    assert _git(repo, "config", "--get", "core.checkStat").stdout.strip() == "minimal"


def test_partial_prior_config_only_reports_changed_key(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    _git(repo, "config", "gc.autoDetach", "false")
    monkeypatch.chdir(repo)

    rc = cg.main([])
    assert rc == 0
    err = capsys.readouterr().err
    assert "core.checkStat=minimal" in err
    assert "set repo gc.autoDetach" not in err
    assert _git(repo, "config", "--get", "core.checkStat").stdout.strip() == "minimal"


def test_unrecognized_first_arg_behaves_as_per_repo_mode(tmp_path, monkeypatch):
    # Review: code-reviewer — Finding 2 (2026-07-22 sidecar): the module docstring's
    # negative-spec claims any first arg other than "--global" is silently treated as
    # "no flag" (per-repo mode), matching the bash oracle's single-value comparison,
    # but no test exercised it — every existing test used [] or ["--global"] only.
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = cg.main(["--globl"])
    assert rc == 0
    assert _git(repo, "config", "--get", "gc.autoDetach").stdout.strip() == "false"
    assert _git(repo, "config", "--get", "core.checkStat").stdout.strip() == "minimal"


def test_config_set_failure_exits_1_with_partial_success(tmp_path, monkeypatch, capsys):
    # Review: code-reviewer — Finding 3 (2026-07-22 sidecar): the documented
    # partial-failure exit path (a failure on the second key exits 1 even if the
    # first key already changed) had zero test coverage — every other test exercised
    # only success paths. Forces the second key's write to fail deterministically.
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)

    real_set = cg._git_config_set

    def fake_set(scope, key, value):
        if key == "core.checkStat":
            return False
        return real_set(scope, key, value)

    monkeypatch.setattr(cg, "_git_config_set", fake_set)

    rc = cg.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ERROR — failed to set core.checkStat=minimal" in err
    assert _git(repo, "config", "--get", "gc.autoDetach").stdout.strip() == "false"
    res = subprocess.run(
        ["git", "config", "--get", "core.checkStat"],
        cwd=str(repo),
        capture_output=True,
        encoding="utf-8",
    )
    assert res.returncode != 0


def test_global_scope_does_not_require_git_repo(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))

    rc = cg.main(["--global"])
    assert rc == 0

    res = subprocess.run(
        ["git", "config", "--global", "--get", "gc.autoDetach"],
        capture_output=True,
        encoding="utf-8",
    )
    assert res.stdout.strip() == "false"

    res2 = subprocess.run(
        ["git", "config", "--global", "--get", "core.checkStat"],
        capture_output=True,
        encoding="utf-8",
    )
    assert res2.stdout.strip() == "minimal"


def test_write_surface_derived_from_settings_not_restated():
    declaration = cg.WRITE_SURFACE
    assert declaration.writer_id == "configure-git"
    assert declaration.source_module == "coordinator_core.ops.configure_git"
    assert len(declaration.clauses) == 1
    clause = declaration.clauses[0]
    assert isinstance(clause, StaticClause)

    declared_keys = {entry.key for entry in clause.entries}
    settings_keys = {key for key, _ in cg._SETTINGS}
    assert declared_keys == settings_keys

    for entry in clause.entries:
        assert entry.kind == "git-config-key"
