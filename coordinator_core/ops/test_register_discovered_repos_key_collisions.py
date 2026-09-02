"""Two candidates that derive one `repos.*` key must not silently overwrite
each other, and the platform's own record wins over a discovered bystander.

Regression for dbc-oduffy/claude-klabauter#2, second half. `_derive_key`
collapses a repo BASENAME, so every clone named `coordinator-claude`
anywhere on the box derives `coordinator_claude`. Registration appended each
candidate and ran a `set` per entry, so the last one written won -- with no
warning, and with no reference to which clone the platform actually loads.
The reporting box had two `coordinator-claude` clones plus a third that was
the live plugin, and `repos.coordinator_claude` ended up naming a clone
Claude Code was not running from.

Note what deduping discovery does NOT fix: two DIFFERENT directories sharing
a basename are not case variants and survive any identity-based dedup. They
still collide on the derived key, which is why this half needs its own fix.

The collision and platform-preference cases below are staged on a
`example-retrieval-repo-plugin` basename rather than the historical `coordinator-claude`
one: `repos.coordinator_claude` is now a retired key (see
`test_retired_key_is_never_registered`), so a fixture using it would exercise
the exclusion instead of the mechanic under test. The shape is unchanged --
a plugin whose declared NAME differs from its clone's directory name.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coordinator_core.ops.register_discovered_repos import main

# `env` is imported for its fixture registration, not called directly -- it
# builds the fake `machine-local` on PATH and sandboxes every registry write.
from coordinator_core.ops.test_register_discovered_repos import (  # noqa: F401
    _read_registry,
    _stub_discover,
    env,
)


@pytest.fixture(autouse=True)
def isolated_claude_home(tmp_path, monkeypatch):
    """Point the installed-plugins lookup at the sandbox.

    Without this the preference step reads the OPERATOR's real
    `~/.claude/plugins/installed_plugins.json`, which would make these
    assertions depend on what is installed on the machine running the suite.
    """
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "plugins").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    return fake_home


def _write_installed_plugins(fake_home: Path, name: str, install_path: str) -> None:
    manifest = fake_home / ".claude" / "plugins" / "installed_plugins.json"
    manifest.write_text(
        json.dumps({"plugins": {f"{name}@some-marketplace": [{"installPath": install_path}]}}),
        encoding="utf-8",
    )


def test_two_distinct_dirs_with_one_derived_key_keep_the_first(
    env, tmp_path, monkeypatch, capsys
):
    """First-wins, not last-wins, and never silently.

    First-wins is the shape that matches the module's standing only-if-absent
    contract: a value already decided is not overwritten by a later one.
    """
    first = str(tmp_path / "clone-a" / "example-retrieval-repo-plugin")
    second = str(tmp_path / "clone-b" / "example-retrieval-repo-plugin")
    os.makedirs(first)
    os.makedirs(second)
    _stub_discover(monkeypatch, [first, second])
    lib_dir, bin_dir = env

    rc = main(["--non-interactive"], self_dir=lib_dir)

    assert rc == 0
    assert _read_registry(bin_dir)["repos.example_retrieval_repo_plugin"] == first
    err = capsys.readouterr().err
    assert "two different directories" in err
    assert second in err, "the ignored path must be named, or the operator cannot act on it"


def test_platform_install_path_beats_a_discovered_bystander(
    env, tmp_path, monkeypatch, isolated_claude_home, capsys
):
    """The reported failure end to end: discovery finds a clone, the platform
    loads a different one, and the registry must name the platform's."""
    discovered = str(tmp_path / "code" / "example-retrieval-repo-plugin")
    live = str(tmp_path / "example-retrieval-repo-plugin")
    os.makedirs(discovered)
    os.makedirs(live)
    # The plugin's declared NAME is `example-retrieval-repo`; its clone is named
    # `example-retrieval-repo-plugin`. That mismatch is the reporting box's shape, and
    # matching on the name rather than the clone basename would miss it.
    _write_installed_plugins(isolated_claude_home, "example-retrieval-repo", live)
    _stub_discover(monkeypatch, [discovered])
    lib_dir, bin_dir = env

    rc = main(["--non-interactive"], self_dir=lib_dir)

    assert rc == 0
    assert _read_registry(bin_dir)["repos.example_retrieval_repo_plugin"] == live
    assert "the platform loads this plugin from" in capsys.readouterr().err


def test_no_correction_when_discovery_already_agrees(
    env, tmp_path, monkeypatch, isolated_claude_home, capsys
):
    """Agreement is silent — a warning on every correct install is noise that
    trains operators to ignore the one that matters."""
    live = str(tmp_path / "example-retrieval-repo-plugin")
    os.makedirs(live)
    _write_installed_plugins(isolated_claude_home, "example-retrieval-repo", live)
    _stub_discover(monkeypatch, [live])
    lib_dir, bin_dir = env

    rc = main(["--non-interactive"], self_dir=lib_dir)

    assert rc == 0
    assert _read_registry(bin_dir)["repos.example_retrieval_repo_plugin"] == live
    assert "the platform loads this plugin from" not in capsys.readouterr().err


def test_absent_manifest_leaves_discovery_untouched(
    env, tmp_path, monkeypatch, isolated_claude_home
):
    """A fresh box with no plugins installed is the normal state, not a fault:
    no manifest means no opinion, never an error or a dropped registration."""
    repo = str(tmp_path / "dev" / "repo-alpha")
    os.makedirs(repo)
    _stub_discover(monkeypatch, [repo])
    lib_dir, bin_dir = env

    rc = main(["--non-interactive"], self_dir=lib_dir)

    assert rc == 0
    assert _read_registry(bin_dir)["repos.repo_alpha"] == repo


def test_malformed_manifest_is_no_opinion_not_a_crash(
    env, tmp_path, monkeypatch, isolated_claude_home
):
    manifest = isolated_claude_home / ".claude" / "plugins" / "installed_plugins.json"
    manifest.write_text("{not json", encoding="utf-8")
    repo = str(tmp_path / "dev" / "repo-alpha")
    os.makedirs(repo)
    _stub_discover(monkeypatch, [repo])
    lib_dir, bin_dir = env

    rc = main(["--non-interactive"], self_dir=lib_dir)

    assert rc == 0
    assert _read_registry(bin_dir)["repos.repo_alpha"] == repo


def test_retired_key_is_never_registered(
    env, tmp_path, monkeypatch, isolated_claude_home, capsys
):
    """`repos.coordinator_claude` is retired by `setup_chain_walker` -- discovery
    finding a `coordinator-claude` clone must not re-provision it.

    Regression for the DoE-claude memo of 2026-09-02: the key came back on a box
    where the operator had unset it, because install re-ran discovery.
    """
    retired = str(tmp_path / "dev" / "coordinator-claude")
    keeper = str(tmp_path / "dev" / "repo-alpha")
    os.makedirs(retired)
    os.makedirs(keeper)
    _stub_discover(monkeypatch, [retired, keeper])
    lib_dir, bin_dir = env

    rc = main(["--non-interactive"], self_dir=lib_dir)

    assert rc == 0
    registry = _read_registry(bin_dir)
    assert "repos.coordinator_claude" not in registry
    assert registry["repos.repo_alpha"] == keeper
    assert "retired key" in capsys.readouterr().err


def test_retired_key_is_not_revived_via_platform_install_path(
    env, tmp_path, monkeypatch, isolated_claude_home
):
    """The platform-preference step must not resurrect the retired key either.

    `installed_plugins.json` records the `coordinator` plugin under a
    `coordinator-claude`-basenamed path, which is exactly how the stray value
    (`~/.claude`) reached the registry on the reporting box.
    """
    plugin_path = str(isolated_claude_home / ".claude")
    _write_installed_plugins(isolated_claude_home, "coordinator", plugin_path)
    discovered = str(tmp_path / "dev" / "coordinator-claude")
    os.makedirs(discovered)
    _stub_discover(monkeypatch, [discovered])
    lib_dir, bin_dir = env

    rc = main(["--non-interactive"], self_dir=lib_dir)

    assert rc == 0
    assert "repos.coordinator_claude" not in _read_registry(bin_dir)


def test_check_only_does_not_preview_a_retired_key(
    env, tmp_path, monkeypatch, isolated_claude_home, capsys
):
    """Preview mode must agree with what registration would do -- otherwise the
    operator is told a key is coming that never lands."""
    retired = str(tmp_path / "dev" / "coordinator-claude")
    os.makedirs(retired)
    _stub_discover(monkeypatch, [retired])
    lib_dir, _bin_dir = env

    rc = main(["--check-only"], self_dir=lib_dir)

    assert rc == 0
    assert "repos.coordinator_claude" not in capsys.readouterr().out
