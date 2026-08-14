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
    doe_root = tmp_path / "doe-claude-clone"
    (doe_root / "coordinator").mkdir(parents=True)
    (config_dir / ".doe-root").write_text(f"{doe_root}\n", encoding="utf-8")
    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    text = run_self_probe(config_dir)

    assert text == ""
    marker = kill_switch_marker_path(str(config_dir / "settings.json"))
    assert not marker.is_file()


def test_sentinel_records_why_an_empty_content_root_is_or_is_not_expected(
    tmp_path: Path, monkeypatch
):
    """The sentinel is a human-facing breadcrumb, and `resolved_ok=false` alone
    is ambiguous: it is the EXPECTED steady state on a `--plugin-dir` or
    marketplace-live box and a genuine fault on a generating one. Read bare, it
    reads as a fault on a healthy machine — that ambiguity cost a real
    investigation (state/audits/2026-08-10-guard-script-fleet-reach-is-not-a-
    publish.md) that got as far as auditing a stood-down kill-switch marker
    before the carve-outs explained the false alarm. So the classification, not
    just the resolution, has to survive to disk.

    Pins all four verdicts. The inline case is the one that matters most: it
    must read `resolved_ok=false` AND be explicitly marked expected."""

    def _verdict_for(config_dir: Path) -> str:
        body = (config_dir / _SENTINEL_NAME).read_text(encoding="utf-8")
        line = [ln for ln in body.splitlines() if ln.startswith("verdict=")]
        assert len(line) == 1, f"expected exactly one verdict line, got {body!r}"
        return line[0].split("=", 1)[1]

    # 1. Content root resolves — the majority path.
    resolved_dir = tmp_path / "resolved" / ".claude"
    resolved_dir.mkdir(parents=True)
    real_root = tmp_path / "resolved" / "coordinator-clone"
    real_root.mkdir(parents=True)
    monkeypatch.setenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, str(real_root))
    run_self_probe(resolved_dir)
    assert _verdict_for(resolved_dir) == "resolved"

    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    # 2. Inline `--plugin-dir`: empty content root is the healthy shape.
    inline_dir = tmp_path / "inline" / ".claude"
    inline_dir.mkdir(parents=True)
    doe_root = tmp_path / "inline" / "doe-claude-clone"
    (doe_root / "coordinator").mkdir(parents=True)
    (inline_dir / ".doe-root").write_text(f"{doe_root}\n", encoding="utf-8")
    run_self_probe(inline_dir)
    body = (inline_dir / _SENTINEL_NAME).read_text(encoding="utf-8")
    assert "resolved_ok=false" in body, "the raw resolution must still be recorded honestly"
    assert _verdict_for(inline_dir) == "expected-inline-plugin-dir"
    assert not kill_switch_marker_path(str(inline_dir / "settings.json")).is_file()

    # 3. Nothing explains it — the fail-safe shape that legitimately arms.
    bare_dir = tmp_path / "bare" / ".claude"
    bare_dir.mkdir(parents=True)
    run_self_probe(bare_dir)
    assert _verdict_for(bare_dir) == "unresolved-and-unexplained"
    assert kill_switch_marker_path(str(bare_dir / "settings.json")).is_file()

    # 4. A stale pointer is NOT an explanation — the carve-out must not blind
    #    the true positive, and the verdict must not launder it as expected.
    stale_dir = tmp_path / "stale" / ".claude"
    stale_dir.mkdir(parents=True)
    (stale_dir / ".doe-root").write_text(
        f"{tmp_path / 'stale' / 'now-gone'}\n", encoding="utf-8"
    )
    run_self_probe(stale_dir)
    assert _verdict_for(stale_dir) == "unresolved-and-unexplained"


def test_stale_doe_root_pointer_still_arms_kill_switch(tmp_path: Path, monkeypatch):
    """Regression: proves the inline-install carve-out does NOT blind the
    true positive. A `.doe-root` pointer file surviving while the coordinator
    directory it points at has actually been destroyed must still arm —
    `is_inline_install` re-verifies the directory exists on disk right now,
    it does not trust the pointer file's mere presence."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    destroyed_root = tmp_path / "doe-claude-clone-now-gone"
    (config_dir / ".doe-root").write_text(f"{destroyed_root}\n", encoding="utf-8")
    # destroyed_root/coordinator is deliberately never created.
    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    text = run_self_probe(config_dir)

    assert "RE-ARMED" in text
    marker = kill_switch_marker_path(str(config_dir / "settings.json"))
    assert marker.is_file()


def test_migrated_doe_root_pointer_stays_silent(tmp_path: Path, monkeypatch):
    """Migrated-box shape (Ask 1, plan 2026-08-07-detector-effective-guard-
    sets.md, C0): the `.doe-root` pointer now lives at
    `<settings-home>/machine-local/.doe-root`, not `{config_dir}/.doe-root`
    (retired — see that plan's C0 body for the cross-machine-clobber
    history). `config_dir` here is a direct sibling of the settings home
    (`home/.claude` next to `home/.coordinator-claude-settings`), the exact
    shape `_settings_home_scoped_to` trusts and `settings_home()`'s own
    default construction produces. No LEGACY `.doe-root` exists at all —
    this fixture is unreachable pre-fix, which is the point: today's code
    only ever reads `config_dir / .doe-root`."""
    home = tmp_path / "home"
    home.mkdir()
    config_dir = home / ".claude"
    config_dir.mkdir()
    settings_home_dir = home / ".coordinator-claude-settings"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    doe_root = tmp_path / "doe-claude-clone"
    (doe_root / "coordinator").mkdir(parents=True)
    ml_dir = settings_home_dir / "machine-local"
    ml_dir.mkdir(parents=True)
    (ml_dir / ".doe-root").write_text(f"{doe_root}\n", encoding="utf-8")
    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    text = run_self_probe(config_dir)

    assert text == ""
    marker = kill_switch_marker_path(str(config_dir / "settings.json"))
    assert not marker.is_file()


def test_migrated_doe_root_empty_pointer_does_not_fall_through_to_legacy(
    tmp_path: Path, monkeypatch
):
    """Empty-migrated-pointer nitpick (Review: staff-eng, finding 9, plan
    2026-08-07-detector-effective-guard-sets.md, C0): presence of the
    migrated FILE suppresses the legacy rung regardless of its contents. A
    migrated pointer that exists but is blank must resolve to False, NOT
    fall through to a legacy pointer that is present and would otherwise
    resolve True."""
    home = tmp_path / "home"
    home.mkdir()
    config_dir = home / ".claude"
    config_dir.mkdir()
    settings_home_dir = home / ".coordinator-claude-settings"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    doe_root = tmp_path / "doe-claude-clone"
    (doe_root / "coordinator").mkdir(parents=True)
    # Legacy rung: present and would resolve True on its own.
    (config_dir / ".doe-root").write_text(f"{doe_root}\n", encoding="utf-8")
    # Migrated rung: present but blank.
    ml_dir = settings_home_dir / "machine-local"
    ml_dir.mkdir(parents=True)
    (ml_dir / ".doe-root").write_text("", encoding="utf-8")
    monkeypatch.delenv(COORDINATOR_CONTENT_ROOT_ENV_KEY, raising=False)

    text = run_self_probe(config_dir)

    assert "RE-ARMED" in text
    marker = kill_switch_marker_path(str(config_dir / "settings.json"))
    assert marker.is_file()


def test_migrated_doe_root_unscoped_config_dir_not_consulted(tmp_path: Path, monkeypatch):
    """Scope-escape guard (Review: staff-eng, finding 4): `machine_local_dir()`
    resolves off ambient env/host state, not `config_dir`. An unrelated
    `config_dir` (not a sibling of the resolved settings home) must NOT pick
    up a live migrated pointer that genuinely exists under that settings
    home — the migrated rung is only consulted when `config_dir` is scoped
    to it. Mirrors `test_ambient_settings_home_unrelated_to_config_dir_is_not_trusted`
    in test_guard_settings_integrity_restore_rungs.py."""
    ambient_home = tmp_path / "unrelated-real-home"
    settings_home_dir = ambient_home / ".coordinator-claude-settings"
    ml_dir = settings_home_dir / "machine-local"
    doe_root = tmp_path / "doe-claude-clone"
    (doe_root / "coordinator").mkdir(parents=True)
    ml_dir.mkdir(parents=True)
    (ml_dir / ".doe-root").write_text(f"{doe_root}\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))

    config_dir = tmp_path / "config"  # NOT a sibling of settings_home_dir
    config_dir.mkdir()
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
