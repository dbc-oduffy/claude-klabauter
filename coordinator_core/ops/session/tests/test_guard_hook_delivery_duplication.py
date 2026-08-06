"""
coordinator_core.ops.session.tests.test_guard_hook_delivery_duplication

Tests for `guard_settings_integrity.detect_hook_delivery_duplication` /
`format_hook_delivery_banner` / `evaluate_hook_delivery_duplication` — the
detect-only double-fire check added 2026-07-28. Asks a different question
than the sibling hook-layer-reachability tests
(`test_guard_settings_integrity_hook_layer.py`): not "is at least one
delivery path live" but "are BOTH live for the SAME hooks", which means
every hook fires twice per session.

Determinism: every test here monkeypatches `guard_settings_integrity.
resolve_content_root` directly (same convention as the sibling reachability
test module) rather than relying on the ambient machine's real coordinator
install — the detector's own duplication logic is exercised, not the
resolver it delegates to.

Negative-spec: this suite does NOT assert anything about repair/write
behavior, because there is none — `detect_hook_delivery_duplication` and
`format_hook_delivery_banner` never touch `settings.json`,
`.coordinator-hooks-disabled`, or any other file. That is the point (see
the module's own "DETECT-ONLY" section docstring); a future PR that adds a
write path here would be a scope violation, not a bug fix.

Spec backlink: example-doctrine-repo dispatch state/subagent-share/
78b683cd-1b62-4a25-904d-954cb3c69412/coordinatorexecutor-8166967b.md
(2026-07-28).
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.session import guard_settings_integrity as gsi
from coordinator_core.resolve_coordinator_clone import ResolveCoordinatorCloneError


def _raise_unresolvable() -> str:
    raise ResolveCoordinatorCloneError("no coordinator content root on this (test) machine")


def _write_hooks_json(content_root: Path, script_rels, write_scripts: bool = True) -> None:
    """Write a `hooks/hooks.json` under `content_root` declaring one
    SessionStart command per entry in `script_rels`, each invoking that entry
    beneath `${CLAUDE_PLUGIN_ROOT}`. When `write_scripts` is True every
    referenced script file is also created on disk."""
    hooks_dir = content_root / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "matcher": "startup",
            "hooks": [
                {
                    "type": "command",
                    "command": f"python3 ${{CLAUDE_PLUGIN_ROOT}}/{rel}",
                    "timeout": 5,
                }
            ],
        }
        for rel in script_rels
    ]
    (hooks_dir / "hooks.json").write_text(
        json.dumps({"hooks": {"SessionStart": entries}}), encoding="utf-8"
    )
    if write_scripts:
        for rel in script_rels:
            script_path = content_root / rel
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")


def _write_settings(config_dir: Path, data: dict) -> Path:
    path = config_dir / "settings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _settings_hooks_block(commands) -> dict:
    return {
        "SessionStart": [
            {
                "matcher": "startup",
                "hooks": [{"type": "command", "command": cmd} for cmd in commands],
            }
        ]
    }


# ---------------------------------------------------------------------------
# Quiet: plugin-only (this machine's real, working configuration — MUST
# pass quiet, per the dispatch brief's requirement #5).
# ---------------------------------------------------------------------------


def test_plugin_only_is_quiet(tmp_path, monkeypatch):
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, ["hooks/scripts/foo.py", "hooks/scripts/bar.py"])
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(config_dir, {"enabledPlugins": {"foo@bar": True}})

    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.plugin_present is True
    assert report.plugin_resolvable is True
    assert report.plugin_entry_count == 2
    assert report.settings_present is False
    assert report.duplicated_scripts == []
    assert report.double_fire is False
    assert gsi.evaluate_hook_delivery_duplication(config_dir) == ""


# ---------------------------------------------------------------------------
# Quiet: settings-only (the pre-`gen_settings_hooks` era / a plugin-side
# outage) — one live path, nothing to duplicate against.
# ---------------------------------------------------------------------------


def test_settings_only_is_quiet(tmp_path, monkeypatch):
    monkeypatch.setattr(gsi, "resolve_content_root", _raise_unresolvable)

    script = tmp_path / "scripts" / "baked-foo.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('hi')\n", encoding="utf-8")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": _settings_hooks_block([f"python3 {script}"]),
        },
    )

    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.plugin_present is False
    assert report.settings_present is True
    assert report.settings_resolvable is True
    assert report.settings_entry_count == 1
    assert report.duplicated_scripts == []
    assert report.double_fire is False
    assert gsi.evaluate_hook_delivery_duplication(config_dir) == ""


# ---------------------------------------------------------------------------
# Quiet: neither path present.
# ---------------------------------------------------------------------------


def test_neither_present_is_quiet(tmp_path, monkeypatch):
    monkeypatch.setattr(gsi, "resolve_content_root", _raise_unresolvable)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(config_dir, {"enabledPlugins": {"foo@bar": True}})

    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.plugin_present is False
    assert report.settings_present is False
    assert report.plugin_entry_count == 0
    assert report.settings_entry_count == 0
    assert report.duplicated_scripts == []
    assert report.double_fire is False
    assert gsi.evaluate_hook_delivery_duplication(config_dir) == ""


# ---------------------------------------------------------------------------
# Double-fire: both surfaces live, declaring the SAME scripts (the
# `gen_settings_hooks.py` identity-filter case this check exists to catch).
# ---------------------------------------------------------------------------


def test_full_overlap_is_double_fire(tmp_path, monkeypatch):
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, ["hooks/scripts/foo.py", "hooks/scripts/bar.py"])
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    foo_baked = content_root / "hooks" / "scripts" / "foo.py"
    bar_baked = content_root / "hooks" / "scripts" / "bar.py"

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": _settings_hooks_block(
                [f"python3 {foo_baked}", f"python3 {bar_baked}"]
            ),
        },
    )

    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.plugin_present is True
    assert report.plugin_resolvable is True
    assert report.plugin_entry_count == 2
    assert report.settings_present is True
    assert report.settings_resolvable is True
    assert report.settings_entry_count == 2
    assert len(report.duplicated_scripts) == 2
    assert report.double_fire is True

    banner = gsi.evaluate_hook_delivery_duplication(config_dir)
    assert banner != ""
    assert "HOOKS ARE FIRING TWICE" in banner
    assert "2 duplicated hook script(s)" in banner
    assert "DETECT-ONLY" in banner
    assert ".coordinator-hooks-disabled" in banner
    assert str(foo_baked.resolve()) in banner
    assert str(bar_baked.resolve()) in banner


# ---------------------------------------------------------------------------
# Partial overlap: both live, but with DIFFERING entry sets — report the
# actual overlap, never assume full duplication from mere presence.
# ---------------------------------------------------------------------------


def test_partial_overlap_reports_honestly(tmp_path, monkeypatch):
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(
        content_root,
        ["hooks/scripts/foo.py", "hooks/scripts/bar.py", "hooks/scripts/baz.py"],
    )
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    foo_baked = content_root / "hooks" / "scripts" / "foo.py"

    only_settings_script = tmp_path / "scripts" / "settings-only.py"
    only_settings_script.parent.mkdir(parents=True)
    only_settings_script.write_text("print('hi')\n", encoding="utf-8")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": _settings_hooks_block(
                [f"python3 {foo_baked}", f"python3 {only_settings_script}"]
            ),
        },
    )

    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.plugin_entry_count == 3
    assert report.settings_entry_count == 2
    assert len(report.duplicated_scripts) == 1
    assert str(foo_baked.resolve()) in report.duplicated_scripts
    assert report.double_fire is True

    banner = gsi.evaluate_hook_delivery_duplication(config_dir)
    assert "1 duplicated hook script(s)" in banner
    # The settings-only script is named too, in the danger section (not the
    # duplicated-scripts list) -- surfacing it is the fix for the unsafe
    # hand-remove-the-hooks-key advice this banner used to give unconditionally.
    assert str(only_settings_script.resolve()) in report.settings_only_scripts
    assert str(only_settings_script.resolve()) in banner
    assert "declared ONLY in settings.json" in banner


# ---------------------------------------------------------------------------
# Never writes: settings.json, the kill-switch marker, and hooks.json are
# byte-identical before and after every detect/evaluate call.
# ---------------------------------------------------------------------------


def test_detect_and_evaluate_never_write_anything(tmp_path, monkeypatch):
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, ["hooks/scripts/foo.py"])
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    foo_baked = content_root / "hooks" / "scripts" / "foo.py"

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings_path = _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": _settings_hooks_block([f"python3 {foo_baked}"]),
        },
    )
    kill_switch = config_dir / ".coordinator-hooks-disabled"
    hooks_json = content_root / "hooks" / "hooks.json"

    settings_before = settings_path.read_bytes()
    hooks_json_before = hooks_json.read_bytes()
    assert not kill_switch.exists()

    gsi.evaluate_hook_delivery_duplication(config_dir)

    assert settings_path.read_bytes() == settings_before
    assert hooks_json.read_bytes() == hooks_json_before
    assert not kill_switch.exists()


# ---------------------------------------------------------------------------
# settings_only_scripts: the asymmetry hazard -- scripts declared ONLY on
# the settings.json surface, invisible to the old detector, which the
# banner must now warn about rather than instruct the operator to delete.
# ---------------------------------------------------------------------------


def test_settings_only_script_reported_and_excluded_from_duplicated(tmp_path, monkeypatch):
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, ["hooks/scripts/foo.py"])
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    foo_baked = content_root / "hooks" / "scripts" / "foo.py"

    only_settings_script = tmp_path / "scripts" / "settings-only.py"
    only_settings_script.parent.mkdir(parents=True)
    only_settings_script.write_text("print('hi')\n", encoding="utf-8")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": _settings_hooks_block(
                [f"python3 {foo_baked}", f"python3 {only_settings_script}"]
            ),
        },
    )

    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.settings_only_scripts == [str(only_settings_script.resolve())]
    assert str(only_settings_script.resolve()) not in report.duplicated_scripts


def test_double_fire_unchanged_by_settings_only_presence(tmp_path, monkeypatch):
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, ["hooks/scripts/foo.py"])
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    foo_baked = content_root / "hooks" / "scripts" / "foo.py"

    only_settings_script = tmp_path / "scripts" / "settings-only.py"
    only_settings_script.parent.mkdir(parents=True)
    only_settings_script.write_text("print('hi')\n", encoding="utf-8")

    # Overlap AND a settings-only script present -- double_fire stays True.
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": _settings_hooks_block(
                [f"python3 {foo_baked}", f"python3 {only_settings_script}"]
            ),
        },
    )
    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert len(report.settings_only_scripts) == 1
    assert report.double_fire is True

    # Disjoint surfaces (no overlap) plus a settings-only script -- double_fire
    # stays False; settings-only-ness never flips this predicate either way.
    content_root2 = tmp_path / "plugin-root-2"
    content_root2.mkdir()
    _write_hooks_json(content_root2, ["hooks/scripts/bar.py"])
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root2))

    config_dir2 = tmp_path / "config2"
    config_dir2.mkdir()
    _write_settings(
        config_dir2,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": _settings_hooks_block([f"python3 {only_settings_script}"]),
        },
    )
    report2 = gsi.detect_hook_delivery_duplication(config_dir2)
    assert report2.duplicated_scripts == []
    assert len(report2.settings_only_scripts) == 1
    assert report2.double_fire is False


def test_banner_renders_settings_only_danger_when_disjoint(tmp_path, monkeypatch):
    """Review: code-reviewer (Finding 1) -- the settings-only danger section
    must render even when `double_fire` is False (disjoint surfaces, zero
    script overlap), not only when both conditions hold together."""
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, ["hooks/scripts/bar.py"])
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    only_settings_script = tmp_path / "scripts" / "settings-only.py"
    only_settings_script.parent.mkdir(parents=True)
    only_settings_script.write_text("print('hi')\n", encoding="utf-8")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": _settings_hooks_block([f"python3 {only_settings_script}"]),
        },
    )

    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.duplicated_scripts == []
    assert len(report.settings_only_scripts) == 1
    assert report.double_fire is False

    banner = gsi.evaluate_hook_delivery_duplication(config_dir)
    assert banner != ""
    assert "declared ONLY in settings.json" in banner
    assert str(only_settings_script.resolve()) in banner
    assert "HOOKS ARE FIRING TWICE" not in banner


def test_banner_with_settings_only_omits_remove_instruction(tmp_path, monkeypatch):
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, ["hooks/scripts/foo.py"])
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    foo_baked = content_root / "hooks" / "scripts" / "foo.py"

    only_settings_script = tmp_path / "scripts" / "settings-only.py"
    only_settings_script.parent.mkdir(parents=True)
    only_settings_script.write_text("print('hi')\n", encoding="utf-8")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": _settings_hooks_block(
                [f"python3 {foo_baked}", f"python3 {only_settings_script}"]
            ),
        },
    )

    banner = gsi.evaluate_hook_delivery_duplication(config_dir)
    assert "declared ONLY in settings.json" in banner
    assert str(only_settings_script.resolve()) in banner
    assert "silently" in banner
    assert "hand-remove" not in banner
    assert "DO NOT DELETE" in banner


def test_banner_without_settings_only_keeps_original_removal_instruction(tmp_path, monkeypatch):
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, ["hooks/scripts/foo.py", "hooks/scripts/bar.py"])
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    foo_baked = content_root / "hooks" / "scripts" / "foo.py"
    bar_baked = content_root / "hooks" / "scripts" / "bar.py"

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": _settings_hooks_block(
                [f"python3 {foo_baked}", f"python3 {bar_baked}"]
            ),
        },
    )

    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.settings_only_scripts == []

    banner = gsi.evaluate_hook_delivery_duplication(config_dir)
    assert "hand-remove the existing" in banner
    assert "declared ONLY in settings.json" not in banner
    assert "DO NOT DELETE" not in banner


def test_settings_only_scripts_defaults_empty_when_surfaces_not_both_present(tmp_path, monkeypatch):
    monkeypatch.setattr(gsi, "resolve_content_root", _raise_unresolvable)

    script = tmp_path / "scripts" / "baked-foo.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('hi')\n", encoding="utf-8")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": _settings_hooks_block([f"python3 {script}"]),
        },
    )

    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.plugin_present is False
    assert report.settings_only_scripts == []
