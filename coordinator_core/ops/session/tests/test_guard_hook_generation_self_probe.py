"""
coordinator_core.ops.session.tests.test_guard_hook_generation_self_probe

Tests for the SessionStart self-probe that records the resolved
COORDINATOR_CONTENT_ROOT to a per-machine sentinel and re-arms the
gen_settings_hooks negative kill switch when that resolution comes back
empty/unresolvable — see that module's own docstring for the incident this
closes (nothing previously ran on EVERY boot to catch a broken hook-delivery
config before the fourth failure taught us).

Import guard: coordinator_core.ops.session.guard_hook_generation_self_probe
MUST be imported at module load time to fire the @register_op(...)
side-effect — done implicitly via the direct `run_self_probe` import.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.install._shared import COORDINATOR_CONTENT_ROOT_ENV_KEY
from coordinator_core.install.gen_settings_hooks import kill_switch_marker_path
from coordinator_core.ops.session.guard_hook_generation_self_probe import (
    _SENTINEL_NAME,
    run_self_probe,
)
from coordinator_core.ops.session.guard_settings_integrity import (
    _read_kill_switch_marker,
)


def _write_installed_plugins(config_dir: Path, plugins: dict) -> None:
    plugins_dir = config_dir / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": plugins}), encoding="utf-8"
    )


def _write_settings_enabled(config_dir: Path, enabled_plugins: dict) -> None:
    (config_dir / "settings.json").write_text(
        json.dumps({"enabledPlugins": enabled_plugins}), encoding="utf-8"
    )


def _make_live_install_dir(tmp_path: Path, name: str = "coordinator-cache") -> Path:
    install_dir = tmp_path / name
    (install_dir / "hooks").mkdir(parents=True)
    (install_dir / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
    return install_dir


def test_healthy_content_root_is_silent_and_records_sentinel(tmp_path: Path, monkeypatch):
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    real_root = tmp_path / "coordinator-clone"
    real_root.mkdir()
    monkeypatch.setenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, str(real_root))

    text = run_self_probe(config_dir)

    assert text == ""
    sentinel = config_dir / _SENTINEL_NAME
    assert sentinel.is_file()
    body = sentinel.read_text(encoding="utf-8")
    assert f"{COORDINATOR_CONTENT_ROOT_ENV_KEY}={real_root}" in body
    assert "resolved_ok=true" in body
    assert not kill_switch_marker_path(str(config_dir / "settings.json")).exists()


def test_empty_content_root_rearms_kill_switch_and_reports(tmp_path: Path, monkeypatch):
    """Re-scoped 2026-07-31 (not inverted — see dispatch report): this
    scenario carries NO `.doe-root` pointer at all, so the inline-install
    carve-out added below does not apply and the fail-safe default (arm)
    still fires — this test's assertion was already correct for this exact
    shape (no positive discriminator evidence whatsoever). Kept, clarified
    via this docstring, to stay distinct from the new plugin-only-shape test
    immediately below it, which DOES carry that evidence and must NOT arm."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    text = run_self_probe(config_dir)

    assert "RE-ARMED" in text
    marker = kill_switch_marker_path(str(config_dir / "settings.json"))
    assert marker.is_file()
    sentinel = config_dir / _SENTINEL_NAME
    assert "resolved_ok=false" in sentinel.read_text(encoding="utf-8")


def test_inline_install_with_empty_content_root_stays_silent(tmp_path: Path, monkeypatch):
    """Plugin-only / inline `--plugin-dir` shape: hook delivery is already
    live from the clone directly, so an empty/unresolvable
    COORDINATOR_CONTENT_ROOT here is the EXPECTED healthy configuration, not
    a broken one — the probe must stay silent and must NOT arm the kill
    switch (see module docstring's 2026-07-31 inline-install carve-out)."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    doe_root = tmp_path / "example-doctrine-repo-clone"
    (doe_root / "coordinator").mkdir(parents=True)
    (config_dir / ".doe-root").write_text(f"{doe_root}\n", encoding="utf-8")
    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    text = run_self_probe(config_dir)

    assert text == ""
    marker = kill_switch_marker_path(str(config_dir / "settings.json"))
    assert not marker.is_file()


def test_stale_doe_root_pointer_still_arms_kill_switch(tmp_path: Path, monkeypatch):
    """Regression: proves the inline-install carve-out does NOT blind the
    true positive. A `.doe-root` pointer file surviving while the coordinator
    directory it points at has actually been destroyed must still arm —
    `is_inline_install` re-verifies the directory exists on disk right now,
    it does not trust the pointer file's mere presence."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    destroyed_root = tmp_path / "example-doctrine-repo-clone-now-gone"
    (config_dir / ".doe-root").write_text(f"{destroyed_root}\n", encoding="utf-8")
    # destroyed_root/coordinator is deliberately never created.
    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    text = run_self_probe(config_dir)

    assert "RE-ARMED" in text
    marker = kill_switch_marker_path(str(config_dir / "settings.json"))
    assert marker.is_file()


def test_rearmed_marker_is_schema_valid_for_sibling_parser(tmp_path: Path, monkeypatch):
    """The marker this probe writes must round-trip through
    `guard_settings_integrity._read_kill_switch_marker` as well-formed (real
    `Since:`/`Expires:` lines) — never landing that sibling parser's
    MALFORMED branch, which a bare `#`-comment-only marker previously did on
    every boot."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    run_self_probe(config_dir)

    marker = kill_switch_marker_path(str(config_dir / "settings.json"))
    info = _read_kill_switch_marker(marker)
    assert info is not None
    assert info.parse_ok is True
    assert info.since is not None
    assert info.expires is not None


def test_content_root_pointing_at_nonexistent_dir_rearms_kill_switch(tmp_path: Path, monkeypatch):
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    monkeypatch.setenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, str(tmp_path / "does-not-exist"))

    text = run_self_probe(config_dir)

    assert "RE-ARMED" in text
    marker = kill_switch_marker_path(str(config_dir / "settings.json"))
    assert marker.is_file()


def test_already_armed_kill_switch_is_silent_on_repeat_boot(tmp_path: Path, monkeypatch):
    """Second bad boot in a row must not re-report — the marker is already
    doing its job, and a SessionStart banner every single boot is noise."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    first = run_self_probe(config_dir)
    assert "RE-ARMED" in first

    second = run_self_probe(config_dir)
    assert second == ""


def test_never_raises_on_unwritable_config_dir(tmp_path: Path, monkeypatch):
    """Fail-open: even if the sentinel/marker writes fail outright, the
    probe must return silently rather than raise (a SessionStart hook that
    raises is exactly the brick class this whole dispatch exists to avoid)."""
    unwritable_parent = tmp_path / "nonexistent-parent" / "also-missing"
    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    # config_dir itself doesn't exist and its parent doesn't either — the
    # sentinel/marker writes must degrade to best-effort failure, not raise.
    text = run_self_probe(unwritable_parent)

    assert isinstance(text, str)


def test_marketplace_install_with_live_install_path_stays_silent(tmp_path: Path, monkeypatch):
    """Marketplace/OSS shape (no `.doe-root` at all): a live `installPath`
    that stats as a real dir with `hooks/hooks.json` present must be read as
    healthy — no re-arm, no marker written."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    install_dir = _make_live_install_dir(tmp_path)
    _write_installed_plugins(
        config_dir,
        {"coordinator@coordinator-claude": [{"installPath": str(install_dir)}]},
    )
    _write_settings_enabled(config_dir, {"coordinator@coordinator-claude": True})
    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    text = run_self_probe(config_dir)

    assert text == ""
    marker = kill_switch_marker_path(str(config_dir / "settings.json"))
    assert not marker.is_file()


def test_marketplace_install_with_missing_install_path_still_arms(tmp_path: Path, monkeypatch):
    """Regression proving the true positive survives: the SAME registry
    record as above, but `installPath` points at a directory that does not
    exist (destroyed tree) -- must still arm the kill switch. The record's
    mere presence is not enough; the stat is load-bearing."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    destroyed_install_dir = tmp_path / "destroyed-coordinator-cache"
    # destroyed_install_dir is deliberately never created.
    _write_installed_plugins(
        config_dir,
        {"coordinator@coordinator-claude": [{"installPath": str(destroyed_install_dir)}]},
    )
    _write_settings_enabled(config_dir, {"coordinator@coordinator-claude": True})
    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    text = run_self_probe(config_dir)

    assert "RE-ARMED" in text
    marker = kill_switch_marker_path(str(config_dir / "settings.json"))
    assert marker.is_file()


def test_marketplace_install_stale_first_record_live_second_record_stays_silent(
    tmp_path: Path, monkeypatch
):
    """Review: code-reviewer (Finding 1) regression. `installed_plugins.json`
    stores a LIST of records per key (e.g. user-scope + project-scope
    installs under the same key) -- a stale/destroyed FIRST record must not
    shadow a live, healthy SECOND record. Iterating only `plugins[key][0]`
    would false-positive-arm here."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    destroyed_install_dir = tmp_path / "destroyed-coordinator-cache"
    # destroyed_install_dir is deliberately never created.
    live_install_dir = _make_live_install_dir(tmp_path)
    _write_installed_plugins(
        config_dir,
        {
            "coordinator@coordinator-claude": [
                {"installPath": str(destroyed_install_dir)},
                {"installPath": str(live_install_dir)},
            ]
        },
    )
    _write_settings_enabled(config_dir, {"coordinator@coordinator-claude": True})
    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    text = run_self_probe(config_dir)

    assert text == ""
    marker = kill_switch_marker_path(str(config_dir / "settings.json"))
    assert not marker.is_file()


def test_marketplace_install_differently_named_marketplace_still_matches(tmp_path: Path, monkeypatch):
    """Matches on the `coordinator@` PREFIX, not the exact
    `coordinator@coordinator-claude` string -- a fork/renamed marketplace
    (e.g. `coordinator@my-fork`) must still be recognized as live."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    install_dir = _make_live_install_dir(tmp_path)
    _write_installed_plugins(
        config_dir,
        {"coordinator@my-fork": [{"installPath": str(install_dir)}]},
    )
    _write_settings_enabled(config_dir, {"coordinator@my-fork": True})
    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    text = run_self_probe(config_dir)

    assert text == ""
    marker = kill_switch_marker_path(str(config_dir / "settings.json"))
    assert not marker.is_file()


def test_marketplace_install_disabled_in_enabled_plugins_still_arms(tmp_path: Path, monkeypatch):
    """A live, stat-confirmed `installPath` is not enough on its own -- if
    `enabledPlugins` marks the key `False` (a deliberate opt-out), the
    carve-out must not treat it as live."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    install_dir = _make_live_install_dir(tmp_path)
    _write_installed_plugins(
        config_dir,
        {"coordinator@coordinator-claude": [{"installPath": str(install_dir)}]},
    )
    _write_settings_enabled(config_dir, {"coordinator@coordinator-claude": False})
    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    text = run_self_probe(config_dir)

    assert "RE-ARMED" in text
    marker = kill_switch_marker_path(str(config_dir / "settings.json"))
    assert marker.is_file()


def test_marketplace_install_malformed_registry_falls_through_silently(tmp_path: Path, monkeypatch):
    """A malformed/absent `installed_plugins.json` must fail open (return
    False from the carve-out) and fall through to the existing (arm)
    behaviour, never raise, never itself add a false silence."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    plugins_dir = config_dir / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "installed_plugins.json").write_text("{not valid json", encoding="utf-8")
    _write_settings_enabled(config_dir, {"coordinator@coordinator-claude": True})
    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    text = run_self_probe(config_dir)

    assert "RE-ARMED" in text
    marker = kill_switch_marker_path(str(config_dir / "settings.json"))
    assert marker.is_file()


def test_handler_op_registered_and_returns_text_dict(tmp_path: Path, monkeypatch):
    import asyncio

    from coordinator_core.ops.session.guard_hook_generation_self_probe import _handler

    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    monkeypatch.setenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, str(tmp_path))  # tmp_path itself is a real dir

    result = asyncio.run(_handler({"config_dir": str(config_dir)}))

    assert result == {"text": ""}
