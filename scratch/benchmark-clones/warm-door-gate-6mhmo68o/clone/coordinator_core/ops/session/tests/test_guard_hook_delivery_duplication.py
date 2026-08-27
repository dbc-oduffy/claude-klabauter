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

Spec backlink: DoE-claude dispatch state/subagent-share/
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
# Indeterminate (NOT quiet): settings-only content, but the content root is
# genuinely unresolvable. Root-cause dispatch state/subagent-share/
# fc858a76-71c8-4e09-9358-3dec6bdff50d/coordinatorexecutor-1acf4e72.md — the
# original "settings-only is quiet" contract this test encoded was itself
# the false-negative half of the confirmed incident: an unresolvable content
# root makes plugin-side hooks.json unlocatable, so "no overlap found" here
# is a resolution FAILURE, not evidence the two surfaces are disjoint. A
# silent (or false "nothing is firing twice") verdict in this exact shape
# hid a real double-fire on the reporting machine. This machine's content
# root genuinely does not resolve, so the correct verdict is an explicit
# INDETERMINATE banner naming the unresolved error, never silence.
# ---------------------------------------------------------------------------


def test_settings_only_with_unresolvable_root_is_indeterminate(tmp_path, monkeypatch):
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
    assert report.indeterminate is True
    assert report.content_root_error is not None
    banner = gsi.evaluate_hook_delivery_duplication(config_dir)
    assert "INDETERMINATE" in banner
    assert "nothing is firing twice today" not in banner
    assert "no coordinator content root on this (test) machine" in banner


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
# Basename/tail overlap: the confirmed incident this dispatch root-caused
# (state/subagent-share/fc858a76-71c8-4e09-9358-3dec6bdff50d/
# coordinatorexecutor-1acf4e72.md) — plugin-side and settings-side resolve
# under two DIFFERENT, each individually-valid, content roots (e.g. a
# SessionStart call vs. an interactive re-run landing on two distinct rungs
# of `resolve_content_root`'s ladder). Exact-absolute-path comparison alone
# reports zero overlap here even though the same scripts are declared on
# both surfaces; overlap must still be detected via the root-independent
# `hooks/scripts/<name>` tail key.
# ---------------------------------------------------------------------------


def test_overlap_detected_across_two_distinct_resolvable_roots(tmp_path, monkeypatch):
    plugin_root = tmp_path / "plugin-root"
    plugin_root.mkdir()
    _write_hooks_json(plugin_root, ["hooks/scripts/foo.py", "hooks/scripts/bar.py"])
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(plugin_root))

    # settings.json's `hooks` block was baked against a SECOND, distinct
    # content root -- both are real, resolvable directories on this
    # (test) machine, neither is empty/unresolvable, but they are not the
    # SAME directory.
    settings_root = tmp_path / "settings-baked-root"
    foo_baked = settings_root / "hooks" / "scripts" / "foo.py"
    bar_baked = settings_root / "hooks" / "scripts" / "bar.py"
    foo_baked.parent.mkdir(parents=True)
    foo_baked.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    bar_baked.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

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
    assert report.settings_present is True
    assert report.settings_resolvable is True
    assert report.indeterminate is False
    # Exact absolute paths differ (different roots) but the tail key
    # (`hooks/scripts/foo.py` / `hooks/scripts/bar.py`) must still match.
    assert len(report.duplicated_scripts) == 2
    assert report.double_fire is True

    banner = gsi.evaluate_hook_delivery_duplication(config_dir)
    assert "HOOKS ARE FIRING TWICE" in banner
    assert "nothing is firing twice today" not in banner


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


# ---------------------------------------------------------------------------
# C3: effective-guard-set join step (AC3, the plan's oracle) and the new
# banner states (AC4, AC5, AC6) that consume the manifest reader C2 shipped
# (`hook_delivery_manifest.read_hook_delivery_manifest`).
# ---------------------------------------------------------------------------


def _write_hooks_json_with_manifest(
    content_root: Path,
    script_rels,
    manifest_block: dict,
    write_scripts: bool = True,
) -> None:
    """Same as `_write_hooks_json`, but embeds the `x-effective-delivery`
    block (C1's contract) alongside the `hooks` key -- the SAME file, one
    parse, matching `read_hook_delivery_manifest`'s "no second read"
    contract."""
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
        json.dumps(
            {
                "hooks": {"SessionStart": entries},
                "x-effective-delivery": manifest_block,
            }
        ),
        encoding="utf-8",
    )
    if write_scripts:
        for rel in script_rels:
            script_path = content_root / rel
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")


def test_carrier_delivered_settings_entries_are_double_fire_not_settings_only(
    tmp_path, monkeypatch
):
    """AC3, the plan's oracle. Replays the memo's exact recorded shape: the
    plugin side delivers ONE carrier (`preuse-write-dispatch.py`) that fans
    out to 4 guards per the manifest; settings.json separately declares 6
    scripts by their OWN baked filename -- 4 of which are exactly the
    carrier's fanned-out guards (settings-side reverse lookup through the
    manifest's `script` field), and 2 of which are genuinely settings-only.
    Without the join step this reports 0 duplicates and 6 settings-only --
    the false negative AC3 exists to kill."""
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    manifest_block = {
        "version": 1,
        "carriers": {
            "scripts/preuse-write-dispatch.py": {
                "guards": [
                    {"id": "guard_one", "script": "scripts/guard-one.py", "tool_names": ["Bash"]},
                    {"id": "guard_two", "script": "scripts/guard-two.py", "tool_names": ["Bash"]},
                    {"id": "guard_three", "script": "scripts/guard-three.py", "tool_names": ["Bash"]},
                    {"id": "guard_four", "script": "scripts/guard-four.py", "tool_names": ["Bash"]},
                ]
            }
        },
        "direct": [],
        "retired": [],
    }
    _write_hooks_json_with_manifest(
        content_root, ["hooks/scripts/preuse-write-dispatch.py"], manifest_block
    )
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    # settings.json declares 6 script commands by their OWN baked filename:
    # 4 are the carrier's fanned-out guards (never named `preuse-write-
    # dispatch.py` directly -- that is the whole point of the incident),
    # and 2 are genuinely settings-only.
    settings_scripts = []
    for name in ("guard-one", "guard-two", "guard-three", "guard-four", "extra-one", "extra-two"):
        p = tmp_path / "scripts" / f"{name}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        settings_scripts.append(p)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": _settings_hooks_block(
                [f"python3 {p}" for p in settings_scripts]
            ),
        },
    )

    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.manifest_state == "ok"
    assert len(report.duplicated_scripts) == 4
    for p in settings_scripts[:4]:
        assert str(p.resolve()) in report.duplicated_scripts
    assert len(report.settings_only_scripts) == 2
    for p in settings_scripts[4:]:
        assert str(p.resolve()) in report.settings_only_scripts
    assert report.double_fire is True

    banner = gsi.evaluate_hook_delivery_duplication(config_dir)
    assert "HOOKS ARE FIRING TWICE" in banner
    assert "4 duplicated hook script(s)" in banner


def test_no_manifest_result_fields_byte_identical_to_filename_comparison(
    tmp_path, monkeypatch
):
    """AC4: with no manifest present (`absent` state), the comparator's
    RESULT FIELDS are byte-identical to today's filename comparison --
    the widening is strictly additive. Reuses the plain (no-manifest)
    fixture shape from `test_partial_overlap_reports_honestly` and asserts
    the exact same result fields."""
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
    assert report.manifest_state == "absent"
    assert report.duplicated_scripts == [str(foo_baked.resolve())]
    assert report.settings_only_scripts == [str(only_settings_script.resolve())]
    assert report.double_fire is True
    assert report.resurrected_decisions == ()


def test_degraded_banner_applicable_mirrors_indeterminate_shape(tmp_path, monkeypatch):
    """AC4's discriminator property: `degraded` is True iff BOTH surfaces
    are live and the manifest state is not `ok` -- mirroring
    `indeterminate`'s own `content_root_error is not None and
    settings_present` shape (both-must-hold, never a single-surface or
    silent verdict)."""
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, ["hooks/scripts/foo.py"])
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    # Plugin-only: settings.json declares no `hooks` block at all.
    _write_settings(config_dir, {"enabledPlugins": {"foo@bar": True}})
    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.settings_present is False
    assert report.degraded is False
    assert gsi.evaluate_hook_delivery_duplication(config_dir) == ""


def test_degraded_banner_renders_when_no_manifest_and_no_other_finding(
    tmp_path, monkeypatch
):
    """AC4: both surfaces live, no manifest, and no overlap/settings-only
    finding at all -- the pre-C3 code returned "" here (silent green); C3
    must render the standalone degraded banner instead, naming the
    filename-only limitation explicitly."""
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, ["hooks/scripts/foo.py"])
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    foo_baked = content_root / "hooks" / "scripts" / "foo.py"

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": _settings_hooks_block([f"python3 {foo_baked}"]),
        },
    )

    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.duplicated_scripts == [str(foo_baked.resolve())]
    assert report.settings_only_scripts == []
    assert report.double_fire is True
    # double_fire True here means this specific case does NOT hit the
    # standalone-degraded path (see next test for that case); assert the
    # discriminator itself instead.
    assert report.degraded is True


def test_standalone_degraded_banner_when_nothing_else_to_report(tmp_path, monkeypatch):
    # Review: coordinator:code-reviewer (P2) -- this test previously
    # asserted the standalone-degraded state was "not achievable without
    # settings entries" and unit-tested `format_hook_delivery_banner`
    # against a hand-built `HookDeliveryReport` on that premise. That claim
    # was false: `settings_present` only requires `hooks_block` to be a
    # non-empty dict (`isinstance(hooks_block, dict) and hooks_block`),
    # while `settings_entry_count`/`settings_commands` come from a SEPARATE
    # recursive `command`-key walk (`_iter_hook_commands`) that legitimately
    # returns `[]` for a non-empty block with no `type: command` hooks --
    # e.g. `{"SessionStart": []}`. Kept as a direct-construction unit test
    # (still earns its place as a fast, isolated check of the banner
    # renderer alone), but see the end-to-end sibling test just below for
    # the real reachability proof through `detect_hook_delivery_duplication`
    # itself.
    report = gsi.HookDeliveryReport(
        plugin_present=True,
        plugin_resolvable=True,
        plugin_entry_count=1,
        settings_present=True,
        settings_resolvable=True,
        settings_entry_count=0,
        duplicated_scripts=[],
        settings_only_scripts=[],
        manifest_state="absent",
    )
    assert report.degraded is True
    banner = gsi.format_hook_delivery_banner(report)
    assert banner != ""
    assert "DEGRADED" in banner
    assert "cannot see guards" in banner
    assert "fan-in" in banner
    assert "DETECT-ONLY" in banner


def test_standalone_degraded_banner_reachable_end_to_end(tmp_path, monkeypatch):
    """End-to-end proof (Review: coordinator:code-reviewer, P2) that the
    standalone-degraded state above is reachable through the REAL detector,
    not just via direct `HookDeliveryReport` construction: settings.json's
    `hooks` block is a non-empty dict (`settings_present=True`) shaped so
    `_iter_hook_commands` walks it to zero `command` entries
    (`settings_entry_count=0`), plugin side is present/resolvable, and no
    manifest block is declared (`manifest_state="absent"`) -- so
    `report.degraded` is True with nothing else to report (no overlap, no
    settings-only scripts, no resurrected decisions), and
    `format_hook_delivery_banner` must render the standalone degraded
    banner rather than the pre-fix silent `""`."""
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, ["hooks/scripts/foo.py"])
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            # Non-empty dict (settings_present=True) with no `type: command`
            # hook entries anywhere under it, so `_iter_hook_commands`
            # legitimately walks it to `[]` (settings_entry_count=0).
            "hooks": {"SessionStart": []},
        },
    )

    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.plugin_present is True
    assert report.settings_present is True
    assert report.settings_entry_count == 0
    assert report.duplicated_scripts == []
    assert report.settings_only_scripts == []
    assert report.resurrected_decisions == ()
    assert report.manifest_state == "absent"
    assert report.degraded is True

    banner = gsi.format_hook_delivery_banner(report)
    assert banner != ""
    assert "DEGRADED" in banner
    assert "cannot see guards" in banner
    assert "fan-in" in banner
    assert "DETECT-ONLY" in banner


def test_stale_manifest_names_unaccounted_command(tmp_path, monkeypatch):
    """AC5: a manifest that does not account for a script-shaped command
    hooks.json declares is reported `stale`, naming the specific
    unaccounted-for command. Under `stale`, positive findings (real
    filename overlap) still render; the unqualified negative claims are
    suppressed exactly as under any non-`ok` state."""
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    # Manifest accounts for NEITHER of the two hooks.json commands -- both
    # are unaccounted, so the manifest is stale.
    manifest_block = {"version": 1, "carriers": {}, "direct": [], "retired": []}
    _write_hooks_json_with_manifest(
        content_root, ["hooks/scripts/foo.py"], manifest_block
    )
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
    assert report.manifest_state == "stale"
    assert report.manifest_unaccounted == ("scripts/foo.py",)
    assert report.degraded is True
    # A settings-only finding is also present here, so the main (pinned)
    # banner body renders -- with the settings-only removal line hedged,
    # per manifest state != `ok` (AC4's narrowed golden-diff scope), not
    # the standalone stale banner (see the next test for that shape).
    banner = gsi.evaluate_hook_delivery_duplication(config_dir)
    assert "AS FAR AS THIS CHECK CAN TELL WITHOUT A DELIVERY MANIFEST" in banner
    assert (
        "Deleting the `hooks` key would stop these running entirely --"
        not in banner
    )


def test_standalone_stale_banner_names_unaccounted_command():
    """AC5's standalone rendering: `stale` state with no other finding at
    all names the specific unaccounted-for command and states the verdict
    is not confident in either direction. Unit-tested directly against
    `format_hook_delivery_banner` (same convention as the standalone
    degraded test above) as a fast, isolated check of the renderer alone.

    Review: coordinator:code-reviewer (P2) -- this test previously claimed
    `stale`-with-zero-findings was "not reachable through
    `detect_hook_delivery_duplication`" because "any settings entry that
    does not overlap always becomes a settings-only finding". That claim
    was false: a settings.json `hooks` block that is a non-empty dict with
    NO `type: command` entries at all (e.g. `{"SessionStart": []}`) yields
    `settings_entry_count=0` with zero settings commands to ever become a
    settings-only finding, while the plugin side can still be `stale`. See
    the end-to-end sibling test just below for the reachability proof
    through the real detector."""
    report = gsi.HookDeliveryReport(
        plugin_present=True,
        plugin_resolvable=True,
        plugin_entry_count=1,
        settings_present=True,
        settings_resolvable=True,
        settings_entry_count=0,
        duplicated_scripts=[],
        settings_only_scripts=[],
        manifest_state="stale",
        manifest_unaccounted=("scripts/foo.py",),
    )
    assert report.degraded is True
    banner = gsi.format_hook_delivery_banner(report)
    assert "STALE" in banner
    assert "scripts/foo.py" in banner
    assert "not confident in either" in banner
    assert "nothing is firing twice today" not in banner


def test_standalone_stale_banner_reachable_end_to_end(tmp_path, monkeypatch):
    """End-to-end proof (Review: coordinator:code-reviewer, P2) that the
    standalone-stale state above is reachable through the REAL detector:
    plugin side declares one script the manifest does not account for
    (`stale`), settings.json's `hooks` block is a non-empty dict with no
    `type: command` entries (`settings_present=True`,
    `settings_entry_count=0`), so nothing overlaps and nothing becomes a
    settings-only finding -- the standalone stale banner must render."""
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    manifest_block = {"version": 1, "carriers": {}, "direct": [], "retired": []}
    _write_hooks_json_with_manifest(
        content_root, ["hooks/scripts/foo.py"], manifest_block
    )
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": {"SessionStart": []},
        },
    )

    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.manifest_state == "stale"
    assert report.manifest_unaccounted == ("scripts/foo.py",)
    assert report.settings_entry_count == 0
    assert report.settings_only_scripts == []
    assert report.duplicated_scripts == []
    assert report.degraded is True

    banner = gsi.evaluate_hook_delivery_duplication(config_dir)
    assert "STALE" in banner
    assert "scripts/foo.py" in banner
    assert "not confident in either" in banner
    assert "nothing is firing twice today" not in banner


def test_resurrected_decision_reported_distinct_from_duplicate_and_settings_only(
    tmp_path, monkeypatch
):
    """AC6: a settings.json entry naming a guard the manifest marks
    `retired` is reported as a resurrected-decision entry, distinct from
    both `duplicated_scripts` and `settings_only_scripts`."""
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    manifest_block = {
        "version": 1,
        "carriers": {},
        "direct": [],
        "retired": [
            {
                "id": "old_guard",
                "script": "scripts/old-guard.py",
                "reason": "superseded by new_guard, ruling 2026-08-01",
            }
        ],
    }
    # hooks.json declares no script-shaped commands at all -- nothing for
    # the exhaustiveness check to be unaccounted-for, so the manifest is
    # `ok`.
    _write_hooks_json_with_manifest(content_root, [], manifest_block)
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    old_guard_script = tmp_path / "scripts" / "old-guard.py"
    old_guard_script.parent.mkdir(parents=True)
    old_guard_script.write_text("print('hi')\n", encoding="utf-8")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": _settings_hooks_block([f"python3 {old_guard_script}"]),
        },
    )

    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.manifest_state == "ok"
    assert len(report.resurrected_decisions) == 1
    decision = report.resurrected_decisions[0]
    assert decision.id == "old_guard"
    assert "old-guard.py" in decision.script
    assert "superseded by new_guard" in decision.reason
    assert report.duplicated_scripts == []
    assert report.settings_only_scripts == []

    banner = gsi.evaluate_hook_delivery_duplication(config_dir)
    assert "RETIRED" in banner
    assert "superseded by new_guard" in banner
    assert "NOT the" in banner and "kill-switch" in banner


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


# ---------------------------------------------------------------------------
# C4, item 1 (original memo Ask 2's second clause): `format_hook_delivery_
# banner` and the kill-switch detail banner (`_double_fire_summary`, which
# `session.guard_hooks_kill_switch_detail` renders through
# `_ks_banner_body`) must not read the same live boot state and render
# contradicting verdicts. Both consuming the same `HookDeliveryReport`
# dataclass instance (C3) prevents disagreement about the report's FIELDS,
# but not about the on-disk fixture each is exercised against in a test --
# that is what this fixture pins. One settings.json + hooks.json is built
# ONCE here and handed to BOTH renderers, so a future change that makes them
# disagree about the SAME boot fails this test rather than shipping.
# ---------------------------------------------------------------------------


def _shared_double_fire_fixture(tmp_path, monkeypatch) -> Path:
    """One on-disk fixture -- a live duplicate script declared on both
    delivery surfaces -- built once and returned as `config_dir`, for both
    `format_hook_delivery_banner` and the kill-switch detail banner
    (`_double_fire_summary`) to render against in the same test."""
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    _write_hooks_json(content_root, ["hooks/scripts/shared-foo.py"])
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    shared_baked = content_root / "hooks" / "scripts" / "shared-foo.py"

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": _settings_hooks_block([f"python3 {shared_baked}"]),
        },
    )
    return config_dir


def test_double_fire_banner_and_kill_switch_detail_agree_on_shared_fixture(
    tmp_path, monkeypatch
):
    """Both banners, exercised against the SAME on-disk fixture, must agree
    that hooks are firing twice this boot -- neither may render a silent or
    contradicting verdict. This is the regression the original memo's Ask 2
    second clause names: `format_hook_delivery_banner` said "nothing is
    firing twice today" on a boot where `guard_hooks_kill_switch_detail`
    said "ALREADY firing twice right now"."""
    config_dir = _shared_double_fire_fixture(tmp_path, monkeypatch)

    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.double_fire is True

    delivery_banner = gsi.format_hook_delivery_banner(report)
    assert "HOOKS ARE FIRING TWICE" in delivery_banner
    assert "nothing is firing twice today" not in delivery_banner

    kill_switch_detail_line = gsi._double_fire_summary(config_dir)
    assert "ALREADY firing twice right now" in kill_switch_detail_line
    assert "could not be checked" not in kill_switch_detail_line


# ---------------------------------------------------------------------------
# C4, item 3 (EM decision): a resurrected-decision finding must render
# ADDITIVELY when it co-occurs with `double_fire` -- not suppressed behind
# it. Resurrected decisions are worse than an ordinary duplicate (a config
# frozen before a retirement ruling silently outranks the ruling), so
# hiding the more severe finding behind the less severe one defeats the
# point. Every existing rendering's text stays byte-intact; this asserts
# BOTH the double-fire body and the resurrected block are present.
# ---------------------------------------------------------------------------


def test_resurrected_decision_renders_additively_alongside_double_fire(
    tmp_path, monkeypatch
):
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    manifest_block = {
        "version": 1,
        "carriers": {},
        "direct": [{"id": "live_dupe", "script": "scripts/live-dupe.py", "tool_names": ["Bash"]}],
        "retired": [
            {
                "id": "old_guard",
                "script": "scripts/old-guard.py",
                "reason": "superseded by new_guard, ruling 2026-08-01",
            }
        ],
    }
    _write_hooks_json_with_manifest(
        content_root, ["hooks/scripts/live-dupe.py"], manifest_block
    )
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    live_dupe_baked = content_root / "hooks" / "scripts" / "live-dupe.py"
    old_guard_script = tmp_path / "scripts" / "old-guard.py"
    old_guard_script.parent.mkdir(parents=True)
    old_guard_script.write_text("print('hi')\n", encoding="utf-8")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "enabledPlugins": {"foo@bar": True},
            "hooks": _settings_hooks_block(
                [f"python3 {live_dupe_baked}", f"python3 {old_guard_script}"]
            ),
        },
    )

    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.manifest_state == "ok"
    assert report.double_fire is True
    assert len(report.resurrected_decisions) == 1

    banner = gsi.evaluate_hook_delivery_duplication(config_dir)
    # The live-duplicate finding (main, pinned body) still renders...
    assert "HOOKS ARE FIRING TWICE" in banner
    # ...AND the resurrected-decision finding renders too, additively --
    # neither suppresses the other.
    assert "RETIRED" in banner
    assert "superseded by new_guard" in banner
    assert "NOT the" in banner and "kill-switch" in banner
