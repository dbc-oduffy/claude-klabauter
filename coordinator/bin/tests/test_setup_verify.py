"""test_setup_verify — pytest tests for coordinator/bin/setup-verify.py.

Spec backlink: example-doctrine-repo coordinator/skills/setup/SKILL.md (Steps 1, 3, 4, 6
  Probes 2/3) — this CLI is the ported destination for that skill's genuine
  imperative bash logic (layout detection, visited-set init, paired-flag
  validation, hooks.json presence check, SKILL.md frontmatter parse,
  settings.json enabledPlugins membership check).

Coverage: one test per subcommand's PASS and FAIL/edge branches, plus the
visited-init stale-cleanup path (a file older than 60 minutes must be deleted;
a fresh file must survive).
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "setup_verify",
        _BIN_DIR / "setup-verify.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------

def test_layout_flat_detected(tmp_path: Path, capsys) -> None:
    (tmp_path / "docs" / "install").mkdir(parents=True)
    (tmp_path / "docs" / "install" / "AGENT.md").write_text("x")
    (tmp_path / "docs" / "install" / "agent-install-manifest.json").write_text("{}")

    ns = _mod.build_parser().parse_args(["layout", "--plugin-root", str(tmp_path)])
    rc = ns.func(ns)
    out = capsys.readouterr().out

    assert rc == 0
    assert "Layout: flat" in out


def test_layout_nested_when_no_flat_agent_md(tmp_path: Path, capsys) -> None:
    (tmp_path / "docs" / "install").mkdir(parents=True)
    (tmp_path / "docs" / "install" / "agent-install-manifest.json").write_text("{}")

    ns = _mod.build_parser().parse_args(["layout", "--plugin-root", str(tmp_path)])
    rc = ns.func(ns)
    out = capsys.readouterr().out

    assert rc == 0
    assert "Layout: nested" in out


def test_layout_missing_manifest_exits_1(tmp_path: Path, capsys) -> None:
    ns = _mod.build_parser().parse_args(["layout", "--plugin-root", str(tmp_path)])
    rc = ns.func(ns)
    err = capsys.readouterr().err

    assert rc == 1
    assert "Manifest not found" in err


# ---------------------------------------------------------------------------
# visited-init
# ---------------------------------------------------------------------------

def test_visited_init_creates_file_with_empty_visited(tmp_path: Path, capsys) -> None:
    ns = _mod.build_parser().parse_args(["visited-init", "--settings-home", str(tmp_path)])
    rc = ns.func(ns)
    out = capsys.readouterr().out

    assert rc == 0
    assert "Session ID:" in out
    assert "Visited-set:" in out

    visited_dir = tmp_path / "coordinator-claude"
    files = list(visited_dir.glob("chain-walk-*.json"))
    assert len(files) == 1

    data = json.loads(files[0].read_text())
    assert data["visited"] == []
    assert "started_at" in data

    # Three-way identity check: the session id must agree across the
    # printed stdout line, the payload's `session_id` field, and the
    # `chain-walk-<sid>.json` filename -- a broken identity thread between
    # any two of these would previously pass under a bare "in data" check.
    stdout_sid = next(
        line.split("Session ID:", 1)[1].strip()
        for line in out.splitlines()
        if "Session ID:" in line
    )
    filename_sid = files[0].stem.removeprefix("chain-walk-")
    assert data["session_id"] == stdout_sid == filename_sid


def test_visited_init_deletes_stale_files(tmp_path: Path) -> None:
    visited_dir = tmp_path / "coordinator-claude"
    visited_dir.mkdir(parents=True)

    stale = visited_dir / "chain-walk-stale.json"
    stale.write_text("{}")
    # Backdate mtime beyond the 60-minute cutoff.
    old_time = time.time() - (61 * 60)
    os.utime(stale, (old_time, old_time))

    fresh = visited_dir / "chain-walk-fresh.json"
    fresh.write_text("{}")

    ns = _mod.build_parser().parse_args(["visited-init", "--settings-home", str(tmp_path)])
    ns.func(ns)

    assert not stale.exists()
    assert fresh.exists()


# ---------------------------------------------------------------------------
# check-override-flags
# ---------------------------------------------------------------------------

def test_override_flags_both_present_ok() -> None:
    rc = _mod.main(["check-override-flags", "--", "--skip-dep-check", "--accept-missing-deps-risk"])
    assert rc == 0


def test_override_flags_neither_present_ok() -> None:
    rc = _mod.main(["check-override-flags", "--", "--check"])
    assert rc == 0


def test_override_flags_only_skip_exits_93(capsys) -> None:
    rc = _mod.main(["check-override-flags", "--", "--skip-dep-check"])
    err = capsys.readouterr().err
    assert rc == 93
    assert "must be passed together" in err


def test_override_flags_only_risk_exits_93(capsys) -> None:
    rc = _mod.main(["check-override-flags", "--", "--accept-missing-deps-risk"])
    assert rc == 93


# ---------------------------------------------------------------------------
# check-hooks
# ---------------------------------------------------------------------------

def _make_plugin_root_with_hooks(tmp_path: Path, hook_rel_paths: list[str], write_files: list[str]) -> Path:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(parents=True)
    hooks_json = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [
                    {"type": "command", "command": f"${{CLAUDE_PLUGIN_ROOT}}/hooks/{p}"}
                    for p in hook_rel_paths
                ]}
            ]
        }
    }
    (hooks_dir / "hooks.json").write_text(json.dumps(hooks_json))
    for f in write_files:
        (hooks_dir / f).write_text("#!/usr/bin/env bash\n")
    return tmp_path


def test_check_hooks_all_present_passes(tmp_path: Path, capsys) -> None:
    _make_plugin_root_with_hooks(tmp_path, ["a.sh", "b.sh"], ["a.sh", "b.sh"])
    ns = _mod.build_parser().parse_args(["check-hooks", "--plugin-root", str(tmp_path)])
    rc = ns.func(ns)
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


def test_check_hooks_missing_fails(tmp_path: Path, capsys) -> None:
    _make_plugin_root_with_hooks(tmp_path, ["a.sh", "missing.sh"], ["a.sh"])
    ns = _mod.build_parser().parse_args(["check-hooks", "--plugin-root", str(tmp_path)])
    rc = ns.func(ns)
    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL" in err
    assert "missing.sh" in err


def test_check_hooks_no_hooks_json_warns(tmp_path: Path, capsys) -> None:
    ns = _mod.build_parser().parse_args(["check-hooks", "--plugin-root", str(tmp_path)])
    rc = ns.func(ns)
    err = capsys.readouterr().err
    assert rc == 0
    assert "WARN" in err


# ---------------------------------------------------------------------------
# check-skill-description
# ---------------------------------------------------------------------------

def test_skill_description_pass(tmp_path: Path, capsys) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text('---\nname: setup\ndescription: "some trigger phrases here"\n---\n\nbody')
    ns = _mod.build_parser().parse_args(["check-skill-description", "--skill-file", str(skill)])
    rc = ns.func(ns)
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


def test_skill_description_missing_field_fails(tmp_path: Path, capsys) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: setup\n---\n\nbody")
    ns = _mod.build_parser().parse_args(["check-skill-description", "--skill-file", str(skill)])
    rc = ns.func(ns)
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out


def test_skill_description_missing_file_fails(tmp_path: Path, capsys) -> None:
    skill = tmp_path / "does-not-exist.md"
    ns = _mod.build_parser().parse_args(["check-skill-description", "--skill-file", str(skill)])
    rc = ns.func(ns)
    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL" in err


# ---------------------------------------------------------------------------
# check-settings-membership
# ---------------------------------------------------------------------------

def test_settings_membership_pass(tmp_path: Path, capsys) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"enabledPlugins": ["coordinator-claude@1.0.0"]}))
    ns = _mod.build_parser().parse_args(["check-settings-membership", "--settings", str(settings)])
    rc = ns.func(ns)
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


def test_settings_membership_fail(tmp_path: Path, capsys) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"enabledPlugins": ["some-other-plugin@1.0.0"]}))
    ns = _mod.build_parser().parse_args(["check-settings-membership", "--settings", str(settings)])
    rc = ns.func(ns)
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out


def test_settings_membership_missing_file_warns(tmp_path: Path, capsys) -> None:
    settings = tmp_path / "does-not-exist.json"
    ns = _mod.build_parser().parse_args(["check-settings-membership", "--settings", str(settings)])
    rc = ns.func(ns)
    err = capsys.readouterr().err
    assert rc == 0
    assert "WARN" in err


# ---------------------------------------------------------------------------
# check-plugin-registered
# ---------------------------------------------------------------------------

def _write_installed_plugins(path: Path, keys: list[str]) -> None:
    path.write_text(json.dumps({
        "version": 2,
        "plugins": {k: [{"scope": "user"}] for k in keys},
    }))


def _write_known_marketplaces(path: Path, names: list[str]) -> None:
    path.write_text(json.dumps({
        n: {"source": {"source": "directory", "path": "/tmp/x"}, "installLocation": "/tmp/x"}
        for n in names
    }))


def test_plugin_registered_pass_when_in_both(tmp_path: Path, capsys) -> None:
    installed = tmp_path / "installed_plugins.json"
    known = tmp_path / "known_marketplaces.json"
    _write_installed_plugins(installed, ["example-retrieval-repo@example-retrieval-repo"])
    _write_known_marketplaces(known, ["example-retrieval-repo"])

    ns = _mod.build_parser().parse_args([
        "check-plugin-registered",
        "--plugin", "example-retrieval-repo",
        "--marketplace", "example-retrieval-repo",
        "--installed-plugins", str(installed),
        "--known-marketplaces", str(known),
    ])
    rc = ns.func(ns)
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


def test_plugin_registered_fails_when_missing_from_installed_plugins(tmp_path: Path, capsys) -> None:
    installed = tmp_path / "installed_plugins.json"
    known = tmp_path / "known_marketplaces.json"
    _write_installed_plugins(installed, ["some-other@example-retrieval-repo"])
    _write_known_marketplaces(known, ["example-retrieval-repo"])

    ns = _mod.build_parser().parse_args([
        "check-plugin-registered",
        "--plugin", "example-retrieval-repo",
        "--marketplace", "example-retrieval-repo",
        "--installed-plugins", str(installed),
        "--known-marketplaces", str(known),
    ])
    rc = ns.func(ns)
    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL" in err
    assert "claude plugin marketplace add example-retrieval-repo" in err
    assert "claude plugin install example-retrieval-repo@example-retrieval-repo" in err


def test_plugin_registered_fails_when_missing_from_known_marketplaces(tmp_path: Path, capsys) -> None:
    installed = tmp_path / "installed_plugins.json"
    known = tmp_path / "known_marketplaces.json"
    _write_installed_plugins(installed, ["example-retrieval-repo@example-retrieval-repo"])
    _write_known_marketplaces(known, ["some-other-marketplace"])

    ns = _mod.build_parser().parse_args([
        "check-plugin-registered",
        "--plugin", "example-retrieval-repo",
        "--marketplace", "example-retrieval-repo",
        "--installed-plugins", str(installed),
        "--known-marketplaces", str(known),
    ])
    rc = ns.func(ns)
    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL" in err
    assert "known_marketplaces.json" in err


def test_plugin_registered_fails_when_directory_exists_but_not_registered(tmp_path: Path, capsys) -> None:
    """Headline regression: a `~/.claude/plugins/<name>/` runtime-data
    directory reads as 'installed' to every eyeball check, but that
    directory alone must never pass this subcommand."""
    installed = tmp_path / "installed_plugins.json"
    known = tmp_path / "known_marketplaces.json"
    _write_installed_plugins(installed, [])
    _write_known_marketplaces(known, [])

    fake_home = tmp_path / "home"
    plugin_dir = fake_home / ".claude" / "plugins" / "example-retrieval-repo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "mcp-session-state").mkdir()

    old_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = str(fake_home)
        ns = _mod.build_parser().parse_args([
            "check-plugin-registered",
            "--plugin", "example-retrieval-repo",
            "--marketplace", "example-retrieval-repo",
            "--installed-plugins", str(installed),
            "--known-marketplaces", str(known),
        ])
        rc = ns.func(ns)
        err = capsys.readouterr().err
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home

    assert rc == 1
    assert "FAIL" in err
    assert "NOT evidence of" in err


def test_plugin_registered_missing_installed_plugins_file_fails(tmp_path: Path, capsys) -> None:
    installed = tmp_path / "does-not-exist.json"
    known = tmp_path / "known_marketplaces.json"
    _write_known_marketplaces(known, ["example-retrieval-repo"])

    ns = _mod.build_parser().parse_args([
        "check-plugin-registered",
        "--plugin", "example-retrieval-repo",
        "--marketplace", "example-retrieval-repo",
        "--installed-plugins", str(installed),
        "--known-marketplaces", str(known),
    ])
    rc = ns.func(ns)
    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL" in err
    assert "installed_plugins.json not found" in err


def test_plugin_registered_missing_known_marketplaces_file_fails(tmp_path: Path, capsys) -> None:
    installed = tmp_path / "installed_plugins.json"
    known = tmp_path / "does-not-exist.json"
    _write_installed_plugins(installed, ["example-retrieval-repo@example-retrieval-repo"])

    ns = _mod.build_parser().parse_args([
        "check-plugin-registered",
        "--plugin", "example-retrieval-repo",
        "--marketplace", "example-retrieval-repo",
        "--installed-plugins", str(installed),
        "--known-marketplaces", str(known),
    ])
    rc = ns.func(ns)
    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL" in err
    assert "known_marketplaces.json not found" in err


def test_plugin_registered_malformed_installed_plugins_fails(tmp_path: Path, capsys) -> None:
    installed = tmp_path / "installed_plugins.json"
    installed.write_text("{not valid json")
    known = tmp_path / "known_marketplaces.json"
    _write_known_marketplaces(known, ["example-retrieval-repo"])

    ns = _mod.build_parser().parse_args([
        "check-plugin-registered",
        "--plugin", "example-retrieval-repo",
        "--marketplace", "example-retrieval-repo",
        "--installed-plugins", str(installed),
        "--known-marketplaces", str(known),
    ])
    rc = ns.func(ns)
    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL" in err
    assert "failed to parse" in err


def test_plugin_registered_weaker_enabledplugins_signal_reported(tmp_path: Path, capsys) -> None:
    installed = tmp_path / "installed_plugins.json"
    known = tmp_path / "known_marketplaces.json"
    _write_installed_plugins(installed, ["example-retrieval-repo@example-retrieval-repo"])
    _write_known_marketplaces(known, ["example-retrieval-repo"])
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"enabledPlugins": {"example-retrieval-repo@example-retrieval-repo": True}}))

    ns = _mod.build_parser().parse_args([
        "check-plugin-registered",
        "--plugin", "example-retrieval-repo",
        "--marketplace", "example-retrieval-repo",
        "--installed-plugins", str(installed),
        "--known-marketplaces", str(known),
        "--settings", str(settings),
    ])
    rc = ns.func(ns)
    err = capsys.readouterr().err
    assert rc == 0
    assert "weaker signal" in err


def _make_live_resolved_source(tmp_path: Path, *, with_manifest: bool, with_commands: bool, with_hooks: bool) -> Path:
    source_dir = tmp_path / "dev-checkout" / "coordinator"
    source_dir.mkdir(parents=True)
    if with_manifest:
        manifest_dir = source_dir / ".claude-plugin"
        manifest_dir.mkdir()
        (manifest_dir / "plugin.json").write_text(json.dumps({"name": "coordinator"}))
    if with_commands:
        commands_dir = source_dir / "commands"
        commands_dir.mkdir()
        (commands_dir / "setup.md").write_text("---\nname: setup\n---\nbody")
    if with_hooks:
        hooks_dir = source_dir / "hooks"
        hooks_dir.mkdir()
        hooks_json = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [
                        {"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/guard.sh"}
                    ]}
                ]
            }
        }
        (hooks_dir / "hooks.json").write_text(json.dumps(hooks_json))
    return source_dir


def test_plugin_registered_state2_pass_live_resolved_via_plugin_dir(tmp_path: Path, capsys) -> None:
    """State 2: unregistered in both registry files, but --plugin-dir points
    at a dev checkout with a manifest and real commands -- must PASS by the
    second legitimate reachability route, not FAIL like a naked directory."""
    installed = tmp_path / "installed_plugins.json"
    known = tmp_path / "known_marketplaces.json"
    _write_installed_plugins(installed, [])
    _write_known_marketplaces(known, [])
    source_dir = _make_live_resolved_source(tmp_path, with_manifest=True, with_commands=True, with_hooks=False)

    ns = _mod.build_parser().parse_args([
        "check-plugin-registered",
        "--plugin", "coordinator",
        "--marketplace", "coordinator-claude",
        "--installed-plugins", str(installed),
        "--known-marketplaces", str(known),
        "--plugin-dir", str(source_dir),
    ])
    rc = ns.func(ns)
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out
    assert "live-resolved" in out


def test_plugin_registered_state2_pass_via_hooks_only(tmp_path: Path, capsys) -> None:
    installed = tmp_path / "installed_plugins.json"
    known = tmp_path / "known_marketplaces.json"
    _write_installed_plugins(installed, [])
    _write_known_marketplaces(known, [])
    source_dir = _make_live_resolved_source(tmp_path, with_manifest=True, with_commands=False, with_hooks=True)

    ns = _mod.build_parser().parse_args([
        "check-plugin-registered",
        "--plugin", "coordinator",
        "--marketplace", "coordinator-claude",
        "--installed-plugins", str(installed),
        "--known-marketplaces", str(known),
        "--plugin-dir", str(source_dir),
    ])
    rc = ns.func(ns)
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


def test_plugin_registered_state3_plugin_dir_without_manifest_still_fails(tmp_path: Path, capsys) -> None:
    """State 3: --plugin-dir given but no manifest present at the resolved
    source is NOT positive evidence -- this stays a hard FAIL (AC-3)."""
    installed = tmp_path / "installed_plugins.json"
    known = tmp_path / "known_marketplaces.json"
    _write_installed_plugins(installed, [])
    _write_known_marketplaces(known, [])
    source_dir = _make_live_resolved_source(tmp_path, with_manifest=False, with_commands=True, with_hooks=True)

    ns = _mod.build_parser().parse_args([
        "check-plugin-registered",
        "--plugin", "coordinator",
        "--marketplace", "coordinator-claude",
        "--installed-plugins", str(installed),
        "--known-marketplaces", str(known),
        "--plugin-dir", str(source_dir),
    ])
    rc = ns.func(ns)
    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL" in err


def test_plugin_registered_state3_manifest_but_no_commands_or_hooks_still_fails(tmp_path: Path, capsys) -> None:
    installed = tmp_path / "installed_plugins.json"
    known = tmp_path / "known_marketplaces.json"
    _write_installed_plugins(installed, [])
    _write_known_marketplaces(known, [])
    source_dir = _make_live_resolved_source(tmp_path, with_manifest=True, with_commands=False, with_hooks=False)

    ns = _mod.build_parser().parse_args([
        "check-plugin-registered",
        "--plugin", "coordinator",
        "--marketplace", "coordinator-claude",
        "--installed-plugins", str(installed),
        "--known-marketplaces", str(known),
        "--plugin-dir", str(source_dir),
    ])
    rc = ns.func(ns)
    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL" in err


def test_plugin_registered_sentinel_alone_never_sufficient(tmp_path: Path, capsys) -> None:
    """AC-2: a dev-repo sentinel file alone (no manifest, no commands, no
    hooks) at --plugin-dir must never become a blanket skip/pass."""
    installed = tmp_path / "installed_plugins.json"
    known = tmp_path / "known_marketplaces.json"
    _write_installed_plugins(installed, [])
    _write_known_marketplaces(known, [])
    source_dir = tmp_path / "dev-checkout"
    source_dir.mkdir()
    (source_dir / ".coordinator-dev-repo").write_text("sentinel only, no manifest, no commands")

    ns = _mod.build_parser().parse_args([
        "check-plugin-registered",
        "--plugin", "coordinator",
        "--marketplace", "coordinator-claude",
        "--installed-plugins", str(installed),
        "--known-marketplaces", str(known),
        "--plugin-dir", str(source_dir),
    ])
    rc = ns.func(ns)
    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL" in err


def test_plugin_registered_state3_no_plugin_dir_given_unregistered_fails(tmp_path: Path, capsys) -> None:
    """No --plugin-dir at all (the plain state-3 case, no route-2 attempt)."""
    installed = tmp_path / "installed_plugins.json"
    known = tmp_path / "known_marketplaces.json"
    _write_installed_plugins(installed, [])
    _write_known_marketplaces(known, [])

    ns = _mod.build_parser().parse_args([
        "check-plugin-registered",
        "--plugin", "coordinator",
        "--marketplace", "coordinator-claude",
        "--installed-plugins", str(installed),
        "--known-marketplaces", str(known),
    ])
    rc = ns.func(ns)
    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL" in err
