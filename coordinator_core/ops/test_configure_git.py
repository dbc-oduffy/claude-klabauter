"""Tests for coordinator_core.ops.configure_git.

Port-parity coverage for coordinator/bin/coordinator-configure-git (DOE-PORT
bin-entrypoint variant).
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.install.write_surface import StaticClause
from coordinator_core.ops import configure_git as cg
from coordinator_core.win_portability import no_console_creationflags

# Declared, not excused: this file spawns a real git process because the
# hardening under test writes real git config (`gc.auto`,
# `core.checkStat`) and asserts idempotence against a real repeat run --
# no mock stands in for real git-config read/write. Each test inits its own
# throwaway repo, so `_init_repo` is not hoisted to module scope -- per-test
# isolation (idempotent-rerun assertions need a known prior-state repo). The
# spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
# explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, encoding="utf-8", check=True,
        **no_console_creationflags(),
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
    assert _git(repo, "config", "--get", "gc.auto").stdout.strip() == "0"
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
    assert _git(repo, "config", "--get", "gc.auto").stdout.strip() == "0"
    assert _git(repo, "config", "--get", "core.checkStat").stdout.strip() == "minimal"


def test_partial_prior_config_only_reports_changed_key(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    _git(repo, "config", "gc.auto", "0")
    monkeypatch.chdir(repo)

    rc = cg.main([])
    assert rc == 0
    err = capsys.readouterr().err
    assert "core.checkStat=minimal" in err
    assert "set repo gc.auto" not in err
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
    assert _git(repo, "config", "--get", "gc.auto").stdout.strip() == "0"
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
    assert _git(repo, "config", "--get", "gc.auto").stdout.strip() == "0"
    res = subprocess.run(
        ["git", "config", "--get", "core.checkStat"],
        cwd=str(repo),
        capture_output=True,
        encoding="utf-8",
        **no_console_creationflags(),
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
        ["git", "config", "--global", "--get", "gc.auto"],
        capture_output=True,
        encoding="utf-8",
        **no_console_creationflags(),
    )
    assert res.stdout.strip() == "0"

    res2 = subprocess.run(
        ["git", "config", "--global", "--get", "core.checkStat"],
        capture_output=True,
        encoding="utf-8",
        **no_console_creationflags(),
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
    settings_keys = {s.key for s in cg._SETTINGS}
    assert declared_keys == settings_keys

    for entry in clause.entries:
        assert entry.kind == "git-config-key"


def test_global_scope_setting_written_machine_wide_from_repo_invocation(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))

    rc = cg.main([])
    assert rc == 0

    res = subprocess.run(
        ["git", "config", "--global", "--get", "core.checkStat"],
        capture_output=True,
        encoding="utf-8",
        **no_console_creationflags(),
    )
    assert res.stdout.strip() == "minimal"

    local = subprocess.run(
        ["git", "config", "--local", "--get", "core.checkStat"],
        cwd=str(repo),
        capture_output=True,
        encoding="utf-8",
        **no_console_creationflags(),
    )
    assert local.returncode != 0


def test_repo_scope_setting_follows_invocation_both_modes(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)

    assert cg.main([]) == 0
    assert (
        _git(repo, "config", "--local", "--get", "gc.auto").stdout.strip()
        == "0"
    )

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    assert cg.main(["--global"]) == 0
    res = subprocess.run(
        ["git", "config", "--global", "--get", "gc.auto"],
        capture_output=True,
        encoding="utf-8",
        **no_console_creationflags(),
    )
    assert res.stdout.strip() == "0"


def test_setting_skipped_for_platform(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)

    fake_setting = cg.GitSetting(
        key="coordinator.fakeplatform",
        value="on",
        platforms=frozenset({"never-a-real-platform"}),
    )
    monkeypatch.setattr(cg, "_SETTINGS", (fake_setting, *cg._SETTINGS))

    rc = cg.main([])
    assert rc == 0
    res = subprocess.run(
        ["git", "config", "--local", "--get", "coordinator.fakeplatform"],
        cwd=str(repo),
        capture_output=True,
        encoding="utf-8",
        **no_console_creationflags(),
    )
    assert res.returncode != 0
    assert _git(repo, "config", "--get", "gc.auto").stdout.strip() == "0"
    assert _git(repo, "config", "--get", "core.checkStat").stdout.strip() == "minimal"


def test_setting_written_for_matching_platform(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)

    fake_setting = cg.GitSetting(
        key="coordinator.fakeplatform",
        value="on",
        platforms=frozenset({sys.platform}),
    )
    monkeypatch.setattr(cg, "_SETTINGS", (fake_setting, *cg._SETTINGS))

    rc = cg.main([])
    assert rc == 0
    assert (
        _git(repo, "config", "--get", "coordinator.fakeplatform").stdout.strip()
        == "on"
    )


_HELP_BROWSER_KEYS = ("help.format", "web.browser", "browser.noop.cmd")


def _stub_git_config(monkeypatch, initial: dict[tuple[tuple[str, ...], str], str]):
    """Stub the git-config subprocess seam with an in-memory store keyed on
    (scope-tuple, key), so triple-write tests don't depend on the host
    platform or touch real git config."""
    store = dict(initial)
    get_calls: list[tuple[tuple[str, ...], str]] = []

    def fake_get(scope, key):
        scope = tuple(scope)
        get_calls.append((scope, key))
        return store.get((scope, key))

    def fake_set(scope, key, value):
        store[(tuple(scope), key)] = value
        return True

    monkeypatch.setattr(cg, "_git_config_get", fake_get)
    monkeypatch.setattr(cg, "_git_config_set", fake_set)
    return store, get_calls


def test_help_browser_triple_written_machine_wide_windows_both_invocations(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "win32")
    for i, argv in enumerate(([], ["--global"])):
        store, _ = _stub_git_config(monkeypatch, {})
        base = tmp_path / f"win-{i}"
        base.mkdir()
        repo = _init_repo(base)
        monkeypatch.chdir(repo)

        rc = cg.main(argv)
        assert rc == 0
        for key in _HELP_BROWSER_KEYS:
            assert store[(("--global",), key)] == dict(
                (s.key, s.value) for s in cg._SETTINGS
            )[key]


def test_help_browser_triple_not_written_on_non_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    for i, argv in enumerate(([], ["--global"])):
        store, _ = _stub_git_config(monkeypatch, {})
        base = tmp_path / f"nonwin-{i}"
        base.mkdir()
        repo = _init_repo(base)
        monkeypatch.chdir(repo)

        rc = cg.main(argv)
        assert rc == 0
        for key in _HELP_BROWSER_KEYS:
            assert (("--global",), key) not in store


def test_help_browser_triple_skipped_when_web_browser_already_set(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "platform", "win32")
    store, _ = _stub_git_config(
        monkeypatch, {(("--global",), "web.browser"): "chrome"}
    )
    base = tmp_path / "preset"
    base.mkdir()
    repo = _init_repo(base)
    monkeypatch.chdir(repo)

    rc = cg.main([])
    assert rc == 0
    err = capsys.readouterr().err
    assert "skipping group 'help-browser'" in err
    assert "web.browser already set to 'chrome'" in err
    assert (("--global",), "help.format") not in store
    assert (("--global",), "browser.noop.cmd") not in store
    assert store[(("--global",), "web.browser")] == "chrome"


def test_help_browser_group_precondition_evaluated_once_per_run(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    _stub_git_config(monkeypatch, {})
    base = tmp_path / "once"
    base.mkdir()
    repo = _init_repo(base)
    monkeypatch.chdir(repo)

    call_count = 0
    real_predicate = cg._help_browser_group_precondition

    def counting_predicate(resolve_scope):
        nonlocal call_count
        call_count += 1
        return real_predicate(resolve_scope)

    monkeypatch.setitem(cg._GROUP_PRECONDITIONS, "help-browser", counting_predicate)

    rc = cg.main([])
    assert rc == 0
    assert call_count == 1


def test_settings_are_gitsetting_records():
    for setting in cg._SETTINGS:
        assert isinstance(setting, cg.GitSetting)

    by_key = {s.key: s for s in cg._SETTINGS}
    assert by_key["gc.auto"].scope == "repo"
    assert by_key["gc.auto"].platforms is None
    assert by_key["gc.auto"].group is None
    assert by_key["gc.auto"].unset_group is None

    # gc.autoDetach only moved auto-gc into the FOREGROUND; gc.auto=0 turns it
    # off. The old key must be gone entirely, not merely joined -- leaving it
    # would keep governing maintenance.autoDetach by fallback.
    assert "gc.autoDetach" not in by_key

    assert by_key["core.checkStat"].scope == "global"
    assert by_key["core.checkStat"].platforms is None
    assert by_key["core.checkStat"].group is None
    assert by_key["core.checkStat"].unset_group is None
