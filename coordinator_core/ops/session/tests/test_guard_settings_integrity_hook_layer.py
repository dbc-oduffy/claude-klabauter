"""
coordinator_core.ops.session.tests.test_guard_settings_integrity_hook_layer

Tests for the hook-layer-reachability conjunct added to
`guard_settings_integrity._is_healthy` (2026-07-28) — the fix for a
completely hook-dead machine reading "healthy" under the pre-existing
`enabledPlugins`-only predicate.

Defect this closes (verified live on a real machine, not hypothetical):
`~/.claude/settings.json` had NO `hooks` key at all, yet passed the old
`_is_healthy` because it only checked `enabledPlugins`. The
`.settings-last-good.json` snapshot such a machine would write (or restore
from) was equally hookless — a stranded machine could be "healed" into a
silently hookless one. `_is_healthy` now ALSO requires
`_hook_layer_reachable`: reachable via plugin-side (`hooks.json` under the
resolved coordinator content root, `coordinator_core.
resolve_coordinator_clone.resolve_content_root` — reused, not
re-implemented) OR settings-side (`settings.json`'s own `hooks` block).

Determinism: every test here monkeypatches
`guard_settings_integrity.resolve_content_root` directly rather than
relying on the ambient machine's actual coordinator install (which this
module's author machine happens to have live, but CI/other machines may
not) — the guard's own reachability logic is exercised, not the resolver
it delegates to (that resolver has its own test coverage).

Negative-spec: does NOT re-test the reconciliation lens (declared-true-vs-
installed plugin keys) — that is orthogonal and lives in
test_guard_settings_integrity.py, which now force-stubs this module's
conjunct via an autouse fixture so its fixtures don't need a resolvable
hook layer at all.

Spec backlink: DoE-claude `state/subagent-share/
78b683cd-1b62-4a25-904d-954cb3c69412/coordinatorexecutor-8cc51fd5.md`
(this fix's originating dispatch, 2026-07-28).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.session import guard_settings_integrity as gsi
from coordinator_core.resolve_coordinator_clone import ResolveCoordinatorCloneError


def _raise_unresolvable() -> str:
    raise ResolveCoordinatorCloneError("no coordinator content root on this (test) machine")


def _write_hooks_json(content_root: Path, script_rel: str, write_script: bool = True) -> None:
    """Write a minimal `hooks/hooks.json` under `content_root` declaring one
    SessionStart command that invokes `script_rel` beneath
    `${CLAUDE_PLUGIN_ROOT}`.
    When `write_script` is True the referenced script file is also created
    (the "resolves" case); when False it is deliberately left absent (the
    "hooks.json present but its referenced scripts do not resolve" case).
    """
    hooks_dir = content_root / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"python3 ${{CLAUDE_PLUGIN_ROOT}}/{script_rel}",
                                    "timeout": 5,
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    if write_script:
        script_path = content_root / script_rel
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")


def _write_settings(config_dir: Path, data: dict) -> Path:
    path = config_dir / "settings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Healthy: plugin-side delivery live, no `hooks` key in settings.json — the
# machine's CURRENT real configuration. This MUST pass.
# ---------------------------------------------------------------------------


def test_plugin_side_live_no_hooks_key_in_settings_is_healthy(tmp_path, monkeypatch):
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, "hooks/scripts/foo.py")
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings_path = _write_settings(config_dir, {"enabledPlugins": {"foo@bar": True}})

    assert gsi._plugin_side_reachable() is True
    assert gsi._is_healthy(settings_path) is True


# ---------------------------------------------------------------------------
# Healthy: settings-side delivery with valid resolvable entries (plugin-side
# unreachable, to isolate the settings-side leg).
# ---------------------------------------------------------------------------


def test_settings_side_valid_entries_is_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(gsi, "resolve_content_root", _raise_unresolvable)

    script = tmp_path / "scripts" / "bar.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('hi')\n", encoding="utf-8")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings_path = _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [{"type": "command", "command": f"python3 {script}"}],
                    }
                ]
            },
        },
    )

    assert gsi._plugin_side_reachable() is False
    assert gsi._is_healthy(settings_path) is True


# ---------------------------------------------------------------------------
# Healthy: both plugin-side and settings-side present and valid.
# ---------------------------------------------------------------------------


def test_both_plugin_and_settings_side_present_is_healthy(tmp_path, monkeypatch):
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, "hooks/scripts/foo.py")
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    settings_script = content_root / "hooks" / "scripts" / "foo.py"  # already exists

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings_path = _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [
                            {"type": "command", "command": f"python3 {settings_script}"}
                        ],
                    }
                ]
            },
        },
    )

    assert gsi._plugin_side_reachable() is True
    assert gsi._is_healthy(settings_path) is True


# ---------------------------------------------------------------------------
# Unhealthy: neither path resolvable.
# ---------------------------------------------------------------------------


def test_neither_path_reachable_is_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setattr(gsi, "resolve_content_root", _raise_unresolvable)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    # enabledPlugins intact, but genuinely no hooks anywhere -- exactly the
    # bug this fix closes: the OLD predicate called this "healthy".
    settings_path = _write_settings(config_dir, {"enabledPlugins": {"foo@bar": True}})

    assert gsi._hook_layer_reachable({"enabledPlugins": {"foo@bar": True}}) is False
    assert gsi._is_healthy(settings_path) is False


# ---------------------------------------------------------------------------
# Unhealthy: hooks.json present but its referenced scripts do not resolve.
# ---------------------------------------------------------------------------


def test_hooks_json_present_but_scripts_missing_is_unhealthy(tmp_path, monkeypatch):
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, "hooks/scripts/ghost.py", write_script=False)
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings_path = _write_settings(config_dir, {"enabledPlugins": {"foo@bar": True}})

    assert gsi._plugin_side_reachable() is False
    assert gsi._is_healthy(settings_path) is False


# ---------------------------------------------------------------------------
# Unhealthy: settings.json hook entries pointing at a nonexistent path (the
# foreign-platform strand -- e.g. a Windows drive-letter path baked into a
# POSIX host's settings.json).
# ---------------------------------------------------------------------------


def test_settings_side_hook_entry_foreign_platform_path_is_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setattr(gsi, "resolve_content_root", _raise_unresolvable)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings_data = {
        "enabledPlugins": {"foo@bar": True},
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 X:/DoE-claude/coordinator/hooks/scripts/foo.py",
                        }
                    ],
                }
            ]
        },
    }
    settings_path = _write_settings(config_dir, settings_data)

    assert gsi._settings_side_reachable(settings_data) is False
    assert gsi._is_healthy(settings_path) is False


# ---------------------------------------------------------------------------
# Unhealthy: malformed / absent settings.json (pre-existing branch,
# unaffected by the hook-layer conjunct -- json.load fails before the
# conjunct is ever evaluated).
# ---------------------------------------------------------------------------


def test_absent_settings_json_is_unhealthy(tmp_path):
    missing = tmp_path / "settings.json"
    assert gsi._is_healthy(missing) is False


def test_malformed_settings_json_is_unhealthy(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert gsi._is_healthy(path) is False


# ---------------------------------------------------------------------------
# Restore-source: a snapshot lacking a reachable hook layer must never be
# selected as a restore source (and settings.json must be left untouched);
# a snapshot with a reachable hook layer must be accepted and restored.
# ---------------------------------------------------------------------------


def test_restore_source_rejected_when_snapshot_hook_layer_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(gsi, "resolve_content_root", _raise_unresolvable)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    # Genuine clobber: no enabledPlugins at all.
    (config_dir / "settings.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    # Snapshot has enabledPlugins but -- like the real machine's snapshot --
    # no hooks key, and the hook layer is unreachable in this test's env.
    (config_dir / ".settings-last-good.json").write_text(
        json.dumps({"enabledPlugins": {"foo@bar": True}}), encoding="utf-8"
    )

    text = gsi.evaluate_settings_integrity(config_dir)

    assert "AUTO-RESTORED" not in text
    assert "no known-good snapshot" in text or "hand-restore" in text
    # settings.json must be left exactly as it was -- never overwritten with
    # a rejected (still-hookless) snapshot.
    assert json.loads((config_dir / "settings.json").read_text(encoding="utf-8")) == {"hooks": {}}


def test_restore_source_accepted_when_snapshot_hook_layer_reachable(tmp_path, monkeypatch):
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, "hooks/scripts/foo.py")
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    (config_dir / ".settings-last-good.json").write_text(
        json.dumps({"enabledPlugins": {"foo@bar": True}}), encoding="utf-8"
    )

    text = gsi.evaluate_settings_integrity(config_dir)

    assert "AUTO-RESTORED" in text
    assert json.loads((config_dir / "settings.json").read_text(encoding="utf-8")) == {
        "enabledPlugins": {"foo@bar": True}
    }


def test_healthy_settings_still_writes_snapshot_via_augmented_predicate(tmp_path, monkeypatch):
    """The write-source gate reuses `_is_healthy` at the same call site as
    before -- confirm the augmented predicate still allows a snapshot write
    when the machine really is fully healthy (both conjuncts true)."""
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, "hooks/scripts/foo.py")
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"foo@bar": True}}), encoding="utf-8"
    )

    text = gsi.evaluate_settings_integrity(config_dir)

    assert "CLOBBERED" not in text
    assert (config_dir / ".settings-last-good.json").is_file()
