"""
coordinator_core.hooks.test_platform_localize -- behavior-parity tests for the
Port of: coordinator/templates/bin/platform-localize.sh (DoE 6fb5fb37,
2026-07-22) SessionStart hook (DOE-PORT Wave B, id: platform-localize).

Tests independently re-derive expected JSON/dict shapes from the bash oracle's
own documented contract (its header comment + embedded Python heredoc logic)
rather than merely re-asserting whatever the port happens to produce -- each
assertion states the expected shape inline so a divergence from the oracle's
documented behavior fails loudly.

Family-I (fresh-install surface): `test_main_fresh_install_smoke` exercises the
hook against a CLAUDE_HOME that has NONE of the tri-file artifacts yet (the
actual first-run shape on a brand-new machine), per PORTER-BRIEF § family_i.

Spec backlink: coordinator_core/hooks/platform_localize.py (port under test).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.hooks import platform_localize as pl  # noqa: E402


# ---------------------------------------------------------------------------
# unit1 -- registry.local.toml parsing + marketplace discovery
# ---------------------------------------------------------------------------

def test_read_registry_local_toml_parses_key_value_rows_only(tmp_path):
    registry_file = tmp_path / "registry.local.toml"
    registry_file.write_text(
        "\n".join(
            [
                "# a comment",
                "[repos]",
                'repos.project_rag = "/Users/x/project-rag"',
                "schema = 1",
                'repos.empty_repo = ""',
                "",
            ]
        )
    )
    registry = pl.read_registry_local_toml(str(registry_file))
    # Oracle regex only understands `key = "value"` -- bare int values and
    # table headers are silently skipped, per the oracle's own comment.
    assert registry == {
        "repos.project_rag": "/Users/x/project-rag",
        "repos.empty_repo": "",
    }
    assert "schema" not in registry


def test_read_registry_local_toml_missing_file_returns_empty_dict(tmp_path):
    assert pl.read_registry_local_toml(str(tmp_path / "nope.toml")) == {}


def test_repo_available_requires_nonempty_and_existing_dir(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    registry = {
        "repos.present": str(real_dir),
        "repos.missing": str(tmp_path / "gone"),
        "repos.blank": "",
    }
    assert pl.repo_available(registry, "repos.present") is True
    assert pl.repo_available(registry, "repos.missing") is False
    assert pl.repo_available(registry, "repos.blank") is False
    assert pl.repo_available(registry, "repos.undeclared") is False


def test_discover_marketplace_dirs_only_matches_claude_plugin_manifest(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    mp_a = plugins_dir / "coordinator-claude"
    (mp_a / ".claude-plugin").mkdir(parents=True)
    non_mp = plugins_dir / "cache"
    non_mp.mkdir()  # no .claude-plugin -- must NOT be discovered
    found = pl.discover_marketplace_dirs(str(plugins_dir), {})
    assert found == {"coordinator-claude": str(mp_a)}


def test_discover_marketplace_dirs_missing_plugins_dir_is_empty(tmp_path):
    assert pl.discover_marketplace_dirs(str(tmp_path / "nope"), {}) == {}


# ---------------------------------------------------------------------------
# unit2 -- settings.local.json build/write
# ---------------------------------------------------------------------------

def test_build_local_settings_emits_extra_known_marketplaces_shape():
    marketplace_dirs = {"coordinator-claude": "/abs/path/coordinator-claude"}
    settings = pl.build_local_settings(marketplace_dirs, {}, {})
    assert settings["extraKnownMarketplaces"] == {
        "coordinator-claude": {
            "source": {"source": "directory", "path": "/abs/path/coordinator-claude"}
        }
    }


def test_build_local_settings_preserves_manual_enabled_plugins_entries():
    existing_local = {"enabledPlugins": {"some-plugin@some-mp": True}}
    settings = pl.build_local_settings({}, existing_local, {})
    assert settings["enabledPlugins"] == {"some-plugin@some-mp": True}


def test_build_local_settings_gates_plugin_when_registry_key_missing(monkeypatch):
    monkeypatch.setattr(pl, "PLUGIN_INFRA_REQUIREMENTS", {"gated-plugin@mp": "repos.missing"})
    try:
        settings = pl.build_local_settings({}, {}, {})
        assert settings["enabledPlugins"] == {"gated-plugin@mp": False}
    finally:
        monkeypatch.setattr(pl, "PLUGIN_INFRA_REQUIREMENTS", {})


def test_build_local_settings_preserves_other_manual_keys():
    existing_local = {"someOtherKey": {"nested": True}}
    settings = pl.build_local_settings({}, existing_local, {})
    assert settings["someOtherKey"] == {"nested": True}
    assert "extraKnownMarketplaces" not in settings
    assert "enabledPlugins" not in settings


def test_write_settings_local_idempotent_no_rewrite_when_unchanged(tmp_path):
    path = tmp_path / "settings.local.json"
    settings = {"extraKnownMarketplaces": {}}
    assert pl.write_settings_local_if_changed(str(path), settings) is True
    mtime_after_first = path.stat().st_mtime_ns
    assert pl.write_settings_local_if_changed(str(path), settings) is False
    assert path.stat().st_mtime_ns == mtime_after_first


def test_atomic_write_preserves_prior_permission_bits(tmp_path):
    """A5 hardened rule: rewriting an existing file must not silently strip
    its prior mode bits (the oracle's bash `cp`-then-mv-adjacent shape
    preserves permissions; a bare os.replace over a fresh 0644 temp file
    would NOT)."""
    # Windows has no POSIX mode bits; os.stat().st_mode & 0o777 is always
    # 0o666 there regardless of the os.chmod() argument. Assert the real
    # invariant under test — mode preserved across the atomic write — by
    # comparing against whatever mode chmod actually produced on this
    # platform, rather than asserting the literal POSIX octal.
    path = tmp_path / "exec_like.json"
    path.write_text("{}")
    os.chmod(path, 0o750)
    mode_before = stat.S_IMODE(os.stat(path).st_mode)
    pl.atomic_write(str(path), '{"a": 1}\n')
    mode_after = stat.S_IMODE(os.stat(path).st_mode)
    assert mode_after == mode_before
    assert json.loads(path.read_text()) == {"a": 1}


# ---------------------------------------------------------------------------
# unit2 -- known_marketplaces.json patch, self-heal, prune
# ---------------------------------------------------------------------------

def test_patch_known_marketplaces_adds_discovered_entry_with_last_updated(tmp_path):
    plugins_dir = tmp_path / "plugins"
    mp_dir = plugins_dir / "coordinator-claude"
    (mp_dir / ".claude-plugin").mkdir(parents=True)
    known_mp_path = tmp_path / "known_marketplaces.json"

    changed, warnings = pl.patch_known_marketplaces(
        str(known_mp_path), {"coordinator-claude": str(mp_dir)}, str(plugins_dir), str(tmp_path)
    )
    assert changed is True
    assert warnings == []
    written = json.loads(known_mp_path.read_text())
    entry = written["coordinator-claude"]
    assert entry["source"] == {"source": "directory", "path": str(mp_dir)}
    assert entry["installLocation"] == str(mp_dir)
    assert isinstance(entry["lastUpdated"], str) and entry["lastUpdated"].endswith("Z")


def test_patch_known_marketplaces_backfills_missing_last_updated_on_url_sourced_entry(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    known_mp_path = tmp_path / "known_marketplaces.json"
    known_mp_path.write_text(
        json.dumps({"some-url-mp": {"source": {"source": "github", "repo": "x/y"}}})
    )
    changed, _warnings = pl.patch_known_marketplaces(str(known_mp_path), {}, str(plugins_dir), str(tmp_path))
    assert changed is True
    written = json.loads(known_mp_path.read_text())
    assert isinstance(written["some-url-mp"]["lastUpdated"], str)


def test_patch_known_marketplaces_no_write_when_nothing_changed(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    known_mp_path = tmp_path / "known_marketplaces.json"
    assert not known_mp_path.exists()
    changed, _warnings = pl.patch_known_marketplaces(str(known_mp_path), {}, str(plugins_dir), str(tmp_path))
    assert changed is False
    assert not known_mp_path.exists()


def test_self_heal_rewrites_missing_clone_path_when_cache_present(tmp_path):
    plugins_dir = tmp_path / "plugins"
    cache_dir = plugins_dir / "cache" / "coordinator-claude"
    cache_dir.mkdir(parents=True)
    existing_km = {
        "coordinator-claude": {
            "source": {"source": "directory", "path": "/nonexistent/clone/path"},
            "installLocation": "/nonexistent/clone/path",
            "lastUpdated": "2020-01-01T00:00:00.000Z",
        }
    }
    changed, warning = pl.self_heal_coordinator_marketplace(
        existing_km, str(plugins_dir), str(tmp_path), "2026-07-17T00:00:00.000Z"
    )
    assert changed is True
    assert warning is not None and "repaired coordinator-claude" in warning
    healed = existing_km["coordinator-claude"]
    assert healed["source"] == {"source": "github", "repo": pl.COORDINATOR_GITHUB_REPO}
    assert healed["installLocation"] == str(plugins_dir / "marketplaces" / "coordinator-claude")


def test_self_heal_leaves_reachable_directory_source_untouched(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    clone_path = tmp_path / "clone"
    clone_path.mkdir()
    existing_km = {
        "coordinator-claude": {
            "source": {"source": "directory", "path": str(clone_path)},
            "installLocation": str(clone_path),
            "lastUpdated": "2020-01-01T00:00:00.000Z",
        }
    }
    before = json.dumps(existing_km, sort_keys=True)
    changed, warning = pl.self_heal_coordinator_marketplace(
        existing_km, str(plugins_dir), str(tmp_path), "2026-07-17T00:00:00.000Z"
    )
    assert changed is False
    assert warning is None
    assert json.dumps(existing_km, sort_keys=True) == before


def test_self_heal_leaves_path_under_claude_home_untouched(tmp_path):
    claude_home = tmp_path / ".claude"
    plugins_dir = claude_home / "plugins"
    cache_dir = plugins_dir / "cache" / "coordinator-claude"
    cache_dir.mkdir(parents=True)
    missing_path_under_home = str(claude_home / "plugins" / "coordinator-claude-gone")
    existing_km = {
        "coordinator-claude": {
            "source": {"source": "directory", "path": missing_path_under_home},
            "installLocation": missing_path_under_home,
            "lastUpdated": "2020-01-01T00:00:00.000Z",
        }
    }
    changed, warning = pl.self_heal_coordinator_marketplace(
        existing_km, str(plugins_dir), str(claude_home), "2026-07-17T00:00:00.000Z"
    )
    assert changed is False
    assert warning is None


def test_prune_removes_stale_directory_entry_but_keeps_url_sourced(tmp_path):
    existing_km = {
        "stale-dir-mp": {"source": {"source": "directory", "path": "/gone"}},
        "url-mp": {"source": {"source": "github", "repo": "x/y"}},
    }
    changed = pl._prune_stale_directory_entries(existing_km, {})
    assert changed is True
    assert "stale-dir-mp" not in existing_km
    assert "url-mp" in existing_km


def test_prune_never_removes_coordinator_claude_entry():
    existing_km = {
        "coordinator-claude": {"source": {"source": "directory", "path": "/gone"}},
    }
    changed = pl._prune_stale_directory_entries(existing_km, {})
    assert changed is False
    assert "coordinator-claude" in existing_km


# ---------------------------------------------------------------------------
# main() -- exit-code contract + fresh-install (Family-I) smoke
# ---------------------------------------------------------------------------

def test_main_fresh_install_smoke(tmp_path, monkeypatch):
    """Family-I: a brand-new machine has NO settings.local.json, NO
    known_marketplaces.json, NO registry.local.toml. main() must run to
    completion (exit 0) and produce well-formed tri-file artifacts, not
    crash on any of the three missing-file paths."""
    fake_home = tmp_path / "fresh-home"
    fake_home.mkdir()
    claude_home = fake_home / ".claude"
    plugins_dir = claude_home / "plugins"
    mp_dir = plugins_dir / "coordinator-claude"
    (mp_dir / ".claude-plugin").mkdir(parents=True)

    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))

    rc = pl.main([])
    assert rc == 0

    settings_local = claude_home / "settings.local.json"
    known_mp = plugins_dir / "known_marketplaces.json"
    assert settings_local.is_file()
    assert known_mp.is_file()

    settings = json.loads(settings_local.read_text())
    assert settings["extraKnownMarketplaces"]["coordinator-claude"]["source"]["path"] == str(mp_dir)

    known = json.loads(known_mp.read_text())
    assert known["coordinator-claude"]["installLocation"] == str(mp_dir)
    assert isinstance(known["coordinator-claude"]["lastUpdated"], str)

    # Idempotency on a second fresh-install-shape run: no unexpected churn.
    rc2 = pl.main([])
    assert rc2 == 0


def test_main_returns_zero_when_nothing_to_do(tmp_path, monkeypatch):
    fake_home = tmp_path / "empty-home"
    fake_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    assert pl.main([]) == 0
    # No plugins dir at all -- must not create settings.local.json with a
    # spurious extraKnownMarketplaces key (oracle only writes the key when
    # extra_mp is non-empty).
    settings_local = fake_home / ".claude" / "settings.local.json"
    if settings_local.is_file():
        assert json.loads(settings_local.read_text()) == {}


def test_main_records_failure_and_returns_1_on_unexpected_exception(tmp_path, monkeypatch):
    fake_home = tmp_path / "boom-home"
    fake_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))

    def _boom(*_args, **_kwargs):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(pl, "run", _boom)

    recorded = {}

    def _fake_record_failure(hook_name, exit_code, detail, log):
        recorded["hook_name"] = hook_name
        recorded["exit_code"] = exit_code
        recorded["detail"] = detail

    monkeypatch.setattr(pl, "_ahs_record_failure", _fake_record_failure)

    rc = pl.main([])
    assert rc == 1
    assert recorded["hook_name"] == "platform-localize"
    assert "injected failure" in recorded["detail"]


# ---------------------------------------------------------------------------
# registry precedence -- settings-home resolution + per-key .local/.toml merge
# ---------------------------------------------------------------------------

def test_read_registry_per_key_precedence(tmp_path):
    """Per-key precedence: registry.local.toml wins per key, tracked
    registry.toml fills gaps (a key only in registry.toml must be visible even
    when registry.local.toml exists), and an empty local value falls through
    (empty-string-is-miss, machine_resolver.registry_get semantics)."""
    local = tmp_path / "registry.local.toml"
    tracked = tmp_path / "registry.toml"
    local.write_text(
        '\n'.join(
            [
                'repos.both = "/local/both"',
                'repos.local_only = "/local/only"',
                'repos.blank_local = ""',
                "",
            ]
        )
    )
    tracked.write_text(
        '\n'.join(
            [
                'repos.both = "/tracked/both"',
                'repos.tracked_only = "/tracked/only"',
                'repos.blank_local = "/tracked/fill"',
                "",
            ]
        )
    )
    merged = pl.read_registry([str(local), str(tracked)])
    assert merged["repos.both"] == "/local/both"
    assert merged["repos.local_only"] == "/local/only"
    assert merged["repos.tracked_only"] == "/tracked/only"
    assert merged["repos.blank_local"] == "/tracked/fill"


def test_resolve_registry_paths_settings_home_not_claude_home(tmp_path, monkeypatch):
    """The registry resolves under SETTINGS-HOME
    (${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings/machine-local), never
    <claude_home>/machine-local -- the claude-home-vs-settings-home trap this
    module previously carried (see module docstring)."""
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    ml = os.path.join(str(tmp_path), ".coordinator-claude-settings", "machine-local")
    assert pl.resolve_registry_paths() == [
        os.path.join(ml, "registry.local.toml"),
        os.path.join(ml, "registry.toml"),
    ]
    override = tmp_path / "override-settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(override))
    assert pl.resolve_registry_paths()[0] == os.path.join(
        str(override), "machine-local", "registry.local.toml"
    )
