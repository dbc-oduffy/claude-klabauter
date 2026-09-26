
from __future__ import annotations

import os

import pytest

from coordinator_core.trusted_root_guard import (
    UntrustedRootError,
    _home_from_env,
    _settings_home_dir_from_env,
    coordinator_trusted_root_guard,
    coordinator_trusted_root_guard_or_exit,
    is_trusted,
)


def _env(**overrides):
    base = {"HOME": "/home/tester", "CLAUDE_HOME": "", "COORDINATOR_PLUGIN_ROOT_TRUSTED": ""}
    base.update(overrides)
    # CLAUDE_HOME empty string should behave like "unset" in the guard's
    # `env.get("CLAUDE_HOME") or env.get("HOME")` fallback chain.
    if base["CLAUDE_HOME"] == "":
        base.pop("CLAUDE_HOME")
    return base


def test_trusted_under_dot_claude_prefix():
    env = _env()
    assert is_trusted("/home/tester/.claude/plugins/coordinator", env=env)


def test_untrusted_outside_any_anchor():
    env = _env()
    assert not is_trusted("/tmp/evil", env=env)


def test_trusted_under_doe_root_sentinel(tmp_path, monkeypatch):
    home = tmp_path
    (home / ".claude").mkdir()
    (home / ".claude" / ".doe-root").write_text(str(tmp_path / "DoE-claude") + "\n")
    env = {"HOME": str(home)}
    assert is_trusted(str(tmp_path / "DoE-claude" / "coordinator"), env=env)


def test_doe_root_itself_is_trusted(tmp_path):
    """A standalone coordinator-claude clone is its own plugin root, so the
    cloud pre-boot passes CLAUDE_PLUGIN_ROOT == repos.doe_claude exactly."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".doe-root").write_text(str(tmp_path / "coordinator-claude") + "\n")
    env = {"HOME": str(tmp_path)}
    assert is_trusted(str(tmp_path / "coordinator-claude"), env=env)
    assert not is_trusted(str(tmp_path / "coordinator-claude-evil"), env=env)


def test_doe_root_trailing_slash_normalized(tmp_path):
    home = tmp_path
    (home / ".claude").mkdir()
    (home / ".claude" / ".doe-root").write_text(str(tmp_path / "DoE-claude") + "/\n")
    env = {"HOME": str(home)}
    assert is_trusted(str(tmp_path / "DoE-claude" / "coordinator"), env=env)


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX-only bash-oracle parity quirk. On Windows the guard normalizes "
    "separators/case for prefix comparison (a `.doe-root` written with forward "
    "slashes must still match a backslashed CLAUDE_PLUGIN_ROOT), which also "
    "collapses this pathological `//` case. Windows behavior is asserted by "
    "test_windows_separator_and_case_normalization below.",
)
def test_doe_root_only_single_trailing_slash_stripped(tmp_path):
    home = tmp_path
    (home / ".claude").mkdir()
    (home / ".claude" / ".doe-root").write_text(str(tmp_path / "DoE-claude") + "//\n")
    env = {"HOME": str(home)}
    assert not is_trusted(str(tmp_path / "DoE-claude" / "coordinator"), env=env)
    assert is_trusted(str(tmp_path / "DoE-claude") + "//coordinator", env=env)


@pytest.mark.skipif(os.name != "nt", reason="Windows path-spelling normalization")
def test_windows_separator_and_case_normalization(tmp_path):
    """Regression: the DoE-clone trust anchor was dead on Windows.

    `.doe-root` is written with forward slashes (`X:/DoE-claude`) while
    CLAUDE_PLUGIN_ROOT arrives from the harness with backslashes
    (`X:\\DoE-claude\\coordinator`), so the textual prefix match never fired and
    the guard fail-loud-rejected a legitimately-trusted dev clone — which
    blocked the documented cold-bootstrap install entirely.
    """
    home = tmp_path
    (home / ".claude").mkdir()
    doe = tmp_path / "DoE-claude"
    (home / ".claude" / ".doe-root").write_text(str(doe).replace("\\", "/") + "\n")
    env = {"HOME": str(home)}

    assert is_trusted(str(doe / "coordinator"), env=env)
    assert is_trusted(str(doe / "coordinator").upper(), env=env)
    assert not is_trusted(str(tmp_path / "DoE-claude-evil" / "coordinator"), env=env)


@pytest.mark.skipif(os.name != "nt", reason="Windows path-spelling normalization")
def test_windows_backslash_traversal_is_rejected(tmp_path):
    env = {"HOME": str(tmp_path)}
    assert not is_trusted(str(tmp_path / ".claude") + "\\..\\..\\tmp\\evil", env=env)


def test_missing_doe_root_sentinel_is_not_an_error(tmp_path):
    env = {"HOME": str(tmp_path)}
    assert not is_trusted(str(tmp_path / "DoE-claude"), env=env)


def test_registry_repos_doe_claude_ranks_above_doe_root_file_mirrors(tmp_path):
    """DR-071: the settings-home registry `repos.doe_claude` key is the
    canonical anchor and must outrank BOTH the durable file mirror and the
    legacy `.doe-root` file — all three deliberately hold DIFFERENT values so
    a false pass (any anchor happening to match) is impossible."""
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)
    registry_root = tmp_path / "from-registry"
    (settings_home_dir / "machine-local" / "registry.local.toml").write_text(
        f"\"repos.doe_claude\" = '{registry_root}'\n"
    )
    durable_root = tmp_path / "from-durable"
    (settings_home_dir / "machine-local" / ".doe-root").write_text(str(durable_root) + "\n")

    home = tmp_path / "home"
    legacy_root = tmp_path / "from-legacy"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".doe-root").write_text(str(legacy_root) + "\n")

    env = {"HOME": str(home), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    assert is_trusted(str(registry_root / "coordinator"), env=env)
    assert not is_trusted(str(durable_root / "coordinator"), env=env)
    assert not is_trusted(str(legacy_root / "coordinator"), env=env)


def test_registry_repos_claude_klabauter_is_trusted_anchor(tmp_path):
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (settings_home_dir / "machine-local" / "registry.local.toml").write_text(
        f"\"repos.claude_klabauter\" = '{claude_klabauter_root}'\n"
    )

    home = tmp_path / "home"
    env = {"HOME": str(home), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    assert is_trusted(str(claude_klabauter_root / "coordinator"), env=env)
    assert not is_trusted(str(tmp_path / "claude-klabauter-evil" / "coordinator"), env=env)


def test_registry_repos_claude_klabauter_durable_pointer_file_fallback(tmp_path):
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (settings_home_dir / "machine-local" / ".claude-klabauter-live-root").write_text(str(claude_klabauter_root) + "\n")

    home = tmp_path / "home"
    env = {"HOME": str(home), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    assert is_trusted(str(claude_klabauter_root / "coordinator"), env=env)


def test_absent_repos_claude_klabauter_key_degrades_cleanly(tmp_path):
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    env = {"HOME": str(home), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    assert not is_trusted(str(tmp_path / "some" / "unrelated" / "root"), env=env)
    assert is_trusted(str(home / ".claude" / "plugins" / "coordinator"), env=env)


def test_a_flat_clone_root_is_trusted_as_itself_not_only_its_children(tmp_path):
    """Regression: a FLAT checkout's own root false-rejected its own anchor.

    The cloud pre-boot install chain registers the published mirror as a
    `directory` marketplace and runs the orchestrator with CLAUDE_PLUGIN_ROOT
    set to that clone's ROOT — the flat shape puts the plugin manifest there,
    not under a `coordinator/` subdirectory. A descendant-only anchor trusted
    `<clone>/coordinator` and rejected `<clone>`, so install-health-run refused
    mid-orchestrator and the whole hook plane was never wired.
    """
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)
    clone = tmp_path / "coordinator-claude"
    (settings_home_dir / "machine-local" / "registry.local.toml").write_text(
        f"\"repos.doe_claude\" = '{clone}'\n"
    )
    env = {"HOME": str(tmp_path / "home"), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    assert is_trusted(str(clone), env=env)
    assert is_trusted(str(clone / "coordinator"), env=env)


def test_the_engine_root_is_trusted_as_itself(tmp_path):
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (settings_home_dir / "machine-local" / "registry.local.toml").write_text(
        f"\"repos.claude_klabauter\" = '{claude_klabauter_root}'\n"
    )
    env = {"HOME": str(tmp_path / "home"), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    assert is_trusted(str(claude_klabauter_root), env=env)


def test_the_widening_stops_at_the_anchor_never_reaches_its_parent(tmp_path):
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)
    clone = tmp_path / "clones" / "coordinator-claude"
    (settings_home_dir / "machine-local" / "registry.local.toml").write_text(
        f"\"repos.doe_claude\" = '{clone}'\n"
    )
    env = {"HOME": str(tmp_path / "home"), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    assert is_trusted(str(clone), env=env)
    assert not is_trusted(str(tmp_path / "clones"), env=env)
    assert not is_trusted(str(tmp_path / "clones" / "coordinator-claude-evil"), env=env)
    assert not is_trusted(str(tmp_path), env=env)


def test_the_marketplace_anchor_stays_descendants_only(tmp_path):
    env = {"HOME": str(tmp_path)}
    assert not is_trusted(str(tmp_path / ".claude"), env=env)
    assert is_trusted(str(tmp_path / ".claude" / "plugins"), env=env)


def test_traversal_segment_rejected_even_under_trusted_prefix():
    env = _env()
    assert not is_trusted("/home/tester/.claude/../../tmp/evil", env=env)


def test_dotdot_prefixed_basename_false_reject_documented():
    # dotdot-PREFIXED (not traversal) segment like `..cache` still matches
    env = _env()
    assert not is_trusted("/home/tester/.claude/..cache", env=env)


def test_plugin_root_trusted_env_opt_out():
    env = _env(COORDINATOR_PLUGIN_ROOT_TRUSTED="1")
    assert is_trusted("/tmp/some/plugin/dir", env=env)


def test_plugin_root_trusted_env_opt_out_overrides_traversal_reset():
    # the `COORDINATOR_PLUGIN_ROOT_TRUSTED=1` opt-out check, so the
    env = _env(COORDINATOR_PLUGIN_ROOT_TRUSTED="1")
    assert is_trusted("/home/tester/.claude/../../tmp/evil", env=env)


# --- home resolution: USERPROFILE rung (F2 regression, 2026-07-28) --------
# F2 (machine-a install dogfood): the guard's home chain was CLAUDE_HOME ->
# HOME with no USERPROFILE rung. HOME is a POSIX convention; native Windows
# shells (PowerShell, cmd.exe) set USERPROFILE instead. With home empty,
# and correct. Every PRE-EXISTING test in this file injects HOME, so this
# USERPROFILE, reproducing a native-Windows shell invocation.


def test_home_from_env_falls_back_to_userprofile_when_home_absent():
    env = {"USERPROFILE": "C:\\Users\\tester"}
    assert _home_from_env(env) == "C:\\Users\\tester"


def test_home_from_env_precedence_claude_home_then_home_then_userprofile():
    env = {"CLAUDE_HOME": "/a", "HOME": "/b", "USERPROFILE": "C:\\c"}
    assert _home_from_env(env) == "/a"
    assert _home_from_env({"HOME": "/b", "USERPROFILE": "C:\\c"}) == "/b"
    assert _home_from_env({"USERPROFILE": "C:\\c"}) == "C:\\c"


def test_home_from_env_empty_when_nothing_set():
    assert _home_from_env({}) == ""


def test_settings_home_resolves_with_only_userprofile_set():
    """The failing configuration itself: no HOME, no CLAUDE_HOME, only
    USERPROFILE (a native PowerShell/cmd.exe environment). Pre-fix this
    returned "" and silently disabled every downstream resolution rung."""
    env = {"USERPROFILE": "C:\\Users\\tester"}
    result = _settings_home_dir_from_env(env)
    assert result == os.path.join("C:\\Users\\tester", ".coordinator-claude-settings")


def test_doe_root_durable_rung_resolves_with_only_userprofile_set(tmp_path):
    """The direct F2 regression: the durable settings-home rung has a
    correct, present value, and the ONLY env var set is USERPROFILE (no
    HOME, no CLAUDE_HOME) -- exactly the shape of a native-Windows shell
    invocation. Pre-fix, `_home_from_env`'s equivalent inline lookup ignored
    USERPROFILE, so `_settings_home_dir_from_env` returned "", which skipped
    this rung (and every other `_doe_root` rung) entirely and made
    `is_trusted` reject a legitimately trusted clone."""
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)
    doe_root = tmp_path / "DoE-claude"
    (settings_home_dir / "machine-local" / ".doe-root").write_text(str(doe_root) + "\n")
    env = {"USERPROFILE": str(tmp_path / "home"), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    assert is_trusted(str(doe_root / "coordinator"), env=env)


def test_settings_home_never_relative_when_home_fully_absent():
    """F10: when home resolves fully empty, the settings-home dir must be
    the loud/empty "" -- never a relative path silently joined against
    whatever cwd the process happens to be invoked from (which produced a
    stray zero-byte file at a Windows drive root, since `os.path.join("",
    ".coordinator-claude-settings")` yields the RELATIVE string
    ".coordinator-claude-settings")."""
    result = _settings_home_dir_from_env({})
    assert result == ""
    assert not (result and not os.path.isabs(result))


# --- coordinator_trusted_root_guard: mode REQUIRED, no default -----------


def test_mode_empty_raises_value_error():
    env = _env()
    with pytest.raises(ValueError):
        coordinator_trusted_root_guard(mode="", root="/tmp/evil", env=env)


def test_mode_unrecognized_raises_value_error():
    env = _env()
    with pytest.raises(ValueError):
        coordinator_trusted_root_guard(mode="fail-quiet", root="/tmp/evil", env=env)


def test_fail_loud_trusted_returns_true():
    env = _env()
    assert coordinator_trusted_root_guard(
        mode="fail-loud", root="/home/tester/.claude/x", env=env
    ) is True


def test_fail_loud_untrusted_raises(capsys):
    env = _env()
    with pytest.raises(UntrustedRootError):
        coordinator_trusted_root_guard(
            mode="fail-loud", root="/tmp/evil", site="test-site", env=env
        )
    err = capsys.readouterr().err
    assert "ERROR" in err
    assert "test-site" in err
    assert "/tmp/evil" in err


def test_fail_loud_diagnostics_show_empty_anchor_and_caveat_override(capsys, tmp_path):
    """Regression for F6 (2026-07-28 Windows install dogfood): a rejection
    with no HOME/USERPROFILE in env must show the empty anchor explicitly,
    and must NOT present COORDINATOR_PLUGIN_ROOT_TRUSTED=1 as an
    unconditional first-choice fix when an anchor resolved empty.

    The caveat is asserted against the diagnostic block that carries it,
    not against the remediation sentence: the remediation moved to a
    runnable installer command on the 2026-08-22 cold-path enrollment
    (`coordinator/tests/test_cold_path_remediation_is_runnable.py`), and an
    assertion pinned to its old wording would fail for a fix, not a
    regression."""
    env = {"COORDINATOR_SETTINGS_HOME": str(tmp_path / "settings-home")}
    with pytest.raises(UntrustedRootError):
        coordinator_trusted_root_guard(mode="fail-loud", root=str(tmp_path / "DoE-claude" / "coordinator"), env=env)
    err = capsys.readouterr().err
    assert "EMPTY" in err
    assert "home:" in err
    assert "doe_root resolved to:" in err
    assert "claude_klabauter_root resolved to:" in err
    assert "registry repos.doe_claude" in err
    assert "scripts/setup.py" in err
    assert "COORDINATOR_PLUGIN_ROOT_TRUSTED=1 would mask it" in err


def test_fail_loud_diagnostics_show_resolved_anchors_when_present(capsys, tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)
    doe_root = tmp_path / "DoE-claude"
    (settings_home_dir / "machine-local" / ".doe-root").write_text(str(doe_root) + "\n")
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (settings_home_dir / "machine-local" / ".claude-klabauter-live-root").write_text(str(claude_klabauter_root) + "\n")
    env = {"HOME": str(home), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    with pytest.raises(UntrustedRootError):
        coordinator_trusted_root_guard(mode="fail-loud", root="/tmp/evil", env=env)
    err = capsys.readouterr().err
    assert "DoE-claude" in err
    assert "at least one anchor above resolved EMPTY" not in err


def test_fail_loud_or_exit_untrusted_calls_sys_exit(capsys):
    env = _env()
    with pytest.raises(SystemExit) as exc_info:
        coordinator_trusted_root_guard_or_exit(
            mode="fail-loud", root="/tmp/evil", env=env
        )
    assert exc_info.value.code == 1


def test_fail_open_trusted_returns_true_no_stderr(capsys):
    env = _env()
    result = coordinator_trusted_root_guard(
        mode="fail-open", root="/home/tester/.claude/x", env=env
    )
    assert result is True
    assert capsys.readouterr().err == ""


def test_fail_open_untrusted_existing_dir_warns(tmp_path, capsys):
    root = str(tmp_path)
    env = _env()
    result = coordinator_trusted_root_guard(mode="fail-open", root=root, env=env)
    assert result is False
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert root in err


def test_fail_open_untrusted_existing_dir_warning_includes_diagnostics(tmp_path, capsys):
    root = str(tmp_path)
    env = _env()
    coordinator_trusted_root_guard(mode="fail-open", root=root, env=env)
    err = capsys.readouterr().err
    assert "doe_root resolved to:" in err


def test_fail_open_untrusted_empty_root_stays_silent(capsys):
    env = _env()
    result = coordinator_trusted_root_guard(mode="fail-open", root="", env=env)
    assert result is False
    assert capsys.readouterr().err == ""


def test_fail_open_untrusted_nonexistent_root_stays_silent(capsys):
    env = _env()
    result = coordinator_trusted_root_guard(
        mode="fail-open", root="/does/not/exist/anywhere", env=env
    )
    assert result is False
    assert capsys.readouterr().err == ""


def test_fail_open_never_raises():
    env = _env()
    result = coordinator_trusted_root_guard(mode="fail-open", root="/tmp/evil", env=env)
    assert result is False


def test_an_empty_anchor_never_trusts_anything(tmp_path):
    env = {"HOME": str(tmp_path / "home")}

    assert not is_trusted("", env=env)
    assert not is_trusted("/tmp/evil", env=env)
    assert not is_trusted("/", env=env)


def test_a_root_only_registry_value_does_not_trust_the_whole_filesystem(tmp_path):
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)
    (settings_home_dir / "machine-local" / "registry.local.toml").write_text(
        "\"repos.doe_claude\" = '/'\n"
    )
    env = {"HOME": str(tmp_path / "home"), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    assert not is_trusted("/tmp/evil", env=env)
    assert not is_trusted("/", env=env)


def test_the_engine_anchor_has_the_same_negative_boundary_as_the_doe_anchor(tmp_path):
    """Anchor 3 carried a positive arm only. The widening is symmetric across
    anchors 2 and 3, so its NEGATIVE boundary must be too — otherwise a
    regression on the engine anchor alone would pass."""
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)
    engine = tmp_path / "clones" / "claude-klabauter"
    (settings_home_dir / "machine-local" / "registry.local.toml").write_text(
        f"\"repos.claude_klabauter\" = '{engine}'\n"
    )
    env = {"HOME": str(tmp_path / "home"), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    assert is_trusted(str(engine), env=env)
    assert not is_trusted(str(tmp_path / "clones"), env=env)
    assert not is_trusted(str(tmp_path / "clones" / "claude-klabauter-evil"), env=env)
    assert not is_trusted(str(tmp_path), env=env)


def test_a_traversal_under_a_widened_anchor_is_still_refused(tmp_path):
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)
    engine = tmp_path / "clones" / "claude-klabauter"
    (settings_home_dir / "machine-local" / "registry.local.toml").write_text(
        f"\"repos.claude_klabauter\" = '{engine}'\n"
    )
    env = {"HOME": str(tmp_path / "home"), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    assert not is_trusted(f"{engine}/..", env=env)
    assert not is_trusted(f"{engine}/../../tmp/evil", env=env)


def _write_plugin_mirror_registry(tmp_path, mirror_root, *, doe_root=None):
    """A settings-home registry naming the SERVED mirror, and optionally a
    DIFFERENT authoring tree — the cloud shape, where the two diverge."""
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True, exist_ok=True)
    lines = [f"\"plugin.mirrors.coordinator-claude.live_path\" = '{mirror_root}'"]
    if doe_root is not None:
        lines.append(f"\"repos.doe_claude\" = '{doe_root}'")
    (settings_home_dir / "machine-local" / "registry.local.toml").write_text(
        "\n".join(lines) + "\n"
    )
    return settings_home_dir


def test_the_served_plugin_mirror_is_a_trust_anchor_of_its_own(tmp_path):
    mirror = tmp_path / "coordinator-claude-mirror"
    authoring = tmp_path / "authoring-tree"
    settings_home_dir = _write_plugin_mirror_registry(tmp_path, mirror, doe_root=authoring)
    env = {"HOME": str(tmp_path / "home"), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    assert is_trusted(str(mirror), env=env)
    assert is_trusted(str(mirror / "coordinator"), env=env)
    assert is_trusted(str(authoring), env=env)


def test_the_mirror_anchor_has_the_same_negative_boundary_as_the_others(tmp_path):
    mirror = tmp_path / "coordinator-claude-mirror"
    settings_home_dir = _write_plugin_mirror_registry(tmp_path, mirror)
    env = {"HOME": str(tmp_path / "home"), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    assert not is_trusted(str(tmp_path), env=env), "never the anchor's parent"
    assert not is_trusted(str(mirror) + "-evil", env=env), "never a name-prefix sibling"
    assert not is_trusted(str(mirror / ".." / ".." / "tmp" / "evil"), env=env)


def test_a_slash_dotdot_poisoned_anchor_value_is_neutralized_by_the_global_reset(tmp_path):
    mirror = tmp_path / "legit" / ".." / "evil-mirror"
    settings_home_dir = _write_plugin_mirror_registry(tmp_path, mirror)
    env = {"HOME": str(tmp_path / "home"), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    poisoned_anchor = str(mirror)
    assert "/.." in poisoned_anchor.replace(os.sep, "/")
    assert not is_trusted(poisoned_anchor, env=env)
    assert not is_trusted(str(mirror / "coordinator"), env=env)


def test_an_absent_mirror_key_contributes_nothing_and_never_raises(tmp_path):
    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)
    (settings_home_dir / "machine-local" / "registry.local.toml").write_text("")
    env = {"HOME": str(tmp_path / "home"), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    assert not is_trusted(str(tmp_path / "coordinator-claude-mirror"), env=env)


def test_diagnostics_name_the_mirror_anchor_without_calling_its_absence_the_defect(
    capsys, tmp_path
):
    settings_home_dir = _write_plugin_mirror_registry(
        tmp_path, tmp_path / "mirror", doe_root=tmp_path / "authoring"
    )
    env = {"HOME": str(tmp_path / "home"), "COORDINATOR_SETTINGS_HOME": str(settings_home_dir)}

    with pytest.raises(UntrustedRootError):
        coordinator_trusted_root_guard(mode="fail-loud", root="/tmp/evil", env=env)

    err = capsys.readouterr().err
    assert "plugin.mirrors.coordinator-claude.live_path" in err
    assert "plugin mirror anchor" in err
