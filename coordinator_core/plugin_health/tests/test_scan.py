"""
coordinator_core.plugin_health.tests.test_scan

Pytest port of example-doctrine-repo coordinator/bin/scan-addon-health.sh (bash oracle,
retired on cutover — see git log). Exercises the reader/consumer of the
plugin_health.sentinel schema: verdict/staleness lines (pass 1),
absent-sentinel detection (pass 2), missing SessionStart hook-script probe
(pass 3), and the `--check-sentinel-presence` fresh-install bootstrap mode.

Regression pin: scan_verdicts() concatenates plugins_root sentinels (sorted)
THEN consumer_root sentinels (sorted) — matching the bash oracle's two-pattern
glob for-loop (`for sentinel in "$PLUGINS_ROOT"/*/... "$CONSUMER_ROOT"/*/...`),
which sorts WITHIN each glob pattern but does not merge across patterns. An
earlier port revision globally string-sorted the merged list, which silently
reordered output whenever the consumer_root path string sorted ahead of the
plugins_root path string (e.g. a "consumer" dir name sorting before "plugins")
— test_scan_verdicts_orders_plugins_root_before_consumer_root pins the correct
(grouped, not merged) shape.

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T3a-g2/T3b
Port of: scan-addon-health.sh (example-doctrine-repo b5a4192c, 2026-07-20)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.plugin_health.scan import (
    _resolve_roots,
    check_sentinel_presence,
    main,
    scan_absent_sentinels,
    scan_missing_hook_scripts,
    scan_verdicts,
)


def _write_sentinel(root: Path, name: str, payload: dict) -> None:
    d = root / name / "data"
    d.mkdir(parents=True, exist_ok=True)
    (d / "doctor-last-run.json").write_text(json.dumps(payload), encoding="utf-8")


def test_scan_verdicts_red_amber_green_stale(tmp_path: Path) -> None:
    plugins_root = tmp_path / "plugins"
    consumer_root = tmp_path / "consumer"
    _write_sentinel(
        plugins_root,
        "pluginA",
        {"verdict": "RED", "ran_at": "2020-01-01T00:00:00Z", "hint": "fix me", "red_probes": ["p1", "p2"]},
    )
    _write_sentinel(
        plugins_root,
        "pluginB",
        {"verdict": "AMBER", "ran_at": "2020-01-01T00:00:00Z", "hint": "aging"},
    )
    _write_sentinel(plugins_root, "pluginC", {"verdict": "GREEN", "ran_at": "2020-01-01T00:00:00Z"})

    now = 1893456000.0  # 2030-01-01, far enough past ran_at to be stale
    lines = scan_verdicts(plugins_root, consumer_root, "--red-and-stale", now, 86400)

    assert any("pluginA: doctor RED (p1,p2) — fix me." in l for l in lines)
    assert any("pluginB: doctor AMBER" in l and "aging" in l for l in lines)
    assert any("pluginC: doctor stale" in l for l in lines)


def test_scan_verdicts_red_only_mode_suppresses_amber_and_stale(tmp_path: Path) -> None:
    plugins_root = tmp_path / "plugins"
    consumer_root = tmp_path / "consumer"
    _write_sentinel(plugins_root, "pluginA", {"verdict": "RED", "ran_at": "2020-01-01T00:00:00Z"})
    _write_sentinel(plugins_root, "pluginB", {"verdict": "AMBER", "ran_at": "2020-01-01T00:00:00Z"})

    lines = scan_verdicts(plugins_root, consumer_root, "--red-only", 1893456000.0, 86400)

    assert len(lines) == 1
    assert "pluginA: doctor RED" in lines[0]


def test_scan_verdicts_malformed_sentinel_red_and_stale_only(tmp_path: Path) -> None:
    plugins_root = tmp_path / "plugins"
    consumer_root = tmp_path / "consumer"
    d = plugins_root / "pluginX" / "data"
    d.mkdir(parents=True)
    (d / "doctor-last-run.json").write_text("not json", encoding="utf-8")

    red_and_stale = scan_verdicts(plugins_root, consumer_root, "--red-and-stale", 1893456000.0, 86400)
    red_only = scan_verdicts(plugins_root, consumer_root, "--red-only", 1893456000.0, 86400)

    assert any("sentinel unreadable" in l for l in red_and_stale)
    assert red_only == []


def test_scan_verdicts_orders_plugins_root_before_consumer_root(tmp_path: Path) -> None:
    """Regression pin — see module docstring note above."""
    plugins_root = tmp_path / "zzz-plugins"
    consumer_root = tmp_path / "aaa-consumer"
    _write_sentinel(plugins_root, "pluginA", {"verdict": "RED", "ran_at": "2020-01-01T00:00:00Z"})
    _write_sentinel(consumer_root, "some-consumer", {"verdict": "RED", "ran_at": "2020-01-01T00:00:00Z"})

    lines = scan_verdicts(plugins_root, consumer_root, "--red-only", 1893456000.0, 86400)

    assert len(lines) == 2
    assert "pluginA" in lines[0]
    assert "some-consumer" in lines[1]


def test_scan_absent_sentinels_reports_declared_but_never_run(tmp_path: Path) -> None:
    plugins_root = tmp_path / "plugins"
    (plugins_root / "pluginC" / "commands").mkdir(parents=True)
    (plugins_root / "pluginC" / "commands" / "doctor.md").write_text("", encoding="utf-8")

    lines = scan_absent_sentinels(plugins_root)

    assert len(lines) == 1
    assert "pluginC: doctor has never run" in lines[0]


def test_scan_absent_sentinels_skips_plugin_with_sentinel(tmp_path: Path) -> None:
    plugins_root = tmp_path / "plugins"
    (plugins_root / "pluginD" / "commands").mkdir(parents=True)
    (plugins_root / "pluginD" / "commands" / "doctor.md").write_text("", encoding="utf-8")
    _write_sentinel(plugins_root, "pluginD", {"verdict": "GREEN", "ran_at": "2020-01-01T00:00:00Z"})

    assert scan_absent_sentinels(plugins_root) == []


def test_scan_absent_sentinels_excludes_backup_dirs(tmp_path: Path) -> None:
    plugins_root = tmp_path / "plugins"
    (plugins_root / "_pre-refresh-snapshots" / "commands").mkdir(parents=True)
    (plugins_root / "_pre-refresh-snapshots" / "commands" / "doctor.md").write_text("", encoding="utf-8")

    assert scan_absent_sentinels(plugins_root) == []


def test_scan_absent_sentinels_no_false_positive_after_settings_home_migration(tmp_path: Path) -> None:
    """DR-072 regression (review Finding 1): a plugin whose install
    (`commands/doctor.md`) stays under legacy PLUGINS_ROOT but whose sentinel
    migrated ONLY to the settings-home lane must produce pass 1's correct
    verdict line and must NOT ALSO produce pass 2's "doctor has never run"
    line in the same --red-and-stale invocation — the two passes must not
    contradict each other."""
    plugins_root = tmp_path / "claude-plugins"
    consumer_root = tmp_path / "claude-consumer"
    sh_plugins_root = tmp_path / "sh" / "plugins"
    sh_consumer_root = tmp_path / "sh"
    (plugins_root / "pluginMigrated" / "commands").mkdir(parents=True)
    (plugins_root / "pluginMigrated" / "commands" / "doctor.md").write_text("", encoding="utf-8")
    _write_sentinel(sh_plugins_root, "pluginMigrated", {"verdict": "GREEN", "ran_at": "2020-01-01T00:00:00Z"})

    verdict_lines = scan_verdicts(
        plugins_root, consumer_root, "--red-and-stale", 1893456000.0, 86400, sh_plugins_root, sh_consumer_root
    )
    absent_lines = scan_absent_sentinels(plugins_root, sh_plugins_root)

    assert any("pluginMigrated" in l for l in verdict_lines)
    assert not any("doctor has never run" in l for l in absent_lines)


def test_scan_missing_hook_scripts_flags_absent_target(tmp_path: Path) -> None:
    plugins_root = tmp_path / "plugins"
    hooks_dir = plugins_root / "pluginE" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"command": '"${CLAUDE_PLUGIN_ROOT}/hooks/missing.sh"'}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    lines = scan_missing_hook_scripts(plugins_root)

    assert len(lines) == 1
    assert "pluginE" in lines[0]
    assert "hooks/missing.sh" in lines[0]


def test_scan_missing_hook_scripts_ok_when_present(tmp_path: Path) -> None:
    plugins_root = tmp_path / "plugins"
    hooks_dir = plugins_root / "pluginF" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "present.sh").write_text("", encoding="utf-8")
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"command": '"${CLAUDE_PLUGIN_ROOT}/hooks/present.sh"'}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    assert scan_missing_hook_scripts(plugins_root) == []


def test_check_sentinel_presence_no_plugins_installed(tmp_path: Path) -> None:
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    assert check_sentinel_presence(plugins_root) is None


def test_check_sentinel_presence_none_written_yet(tmp_path: Path) -> None:
    plugins_root = tmp_path / "plugins"
    (plugins_root / "pluginA").mkdir(parents=True)

    msg = check_sentinel_presence(plugins_root)

    assert msg is not None
    assert "no doctor sentinels found" in msg


def test_check_sentinel_presence_at_least_one_sentinel(tmp_path: Path) -> None:
    plugins_root = tmp_path / "plugins"
    _write_sentinel(plugins_root, "pluginA", {"verdict": "GREEN", "ran_at": "2020-01-01T00:00:00Z"})

    assert check_sentinel_presence(plugins_root) is None


def test_check_sentinel_presence_no_false_positive_after_settings_home_migration(tmp_path: Path) -> None:
    """DR-072 regression (coordinator follow-on, same defect class as review
    Finding 1): plugins installed under legacy plugins_root with sentinels
    present ONLY in the settings-home plugins lane must NOT trigger the
    "no doctor sentinels found ... run /coordinator:install" bootstrap nag —
    that lane counts toward "a sentinel exists anywhere"."""
    plugins_root = tmp_path / "plugins"
    sh_plugins_root = tmp_path / "sh" / "plugins"
    (plugins_root / "pluginA").mkdir(parents=True)
    (plugins_root / "pluginB").mkdir(parents=True)
    _write_sentinel(sh_plugins_root, "pluginA", {"verdict": "GREEN", "ran_at": "2020-01-01T00:00:00Z"})
    _write_sentinel(sh_plugins_root, "pluginB", {"verdict": "GREEN", "ran_at": "2020-01-01T00:00:00Z"})

    assert check_sentinel_presence(plugins_root, sh_plugins_root) is None


def test_main_unknown_mode_returns_exit_2(capsys: pytest.CaptureFixture) -> None:
    rc = main(["--bogus-mode"])

    assert rc == 2
    captured = capsys.readouterr()
    assert "unknown mode" in captured.err


def test_main_no_roots_present_returns_exit_0_silent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("COORDINATOR_PLUGINS_ROOT", str(tmp_path / "does-not-exist-plugins"))
    monkeypatch.setenv("COORDINATOR_CONSUMER_HEALTH_ROOT", str(tmp_path / "does-not-exist-consumer"))

    rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""


# ---------------------------------------------------------------------------
# _resolve_roots — §4a CLAUDE_HOME convention + settings-home dual-read lane
# ---------------------------------------------------------------------------
#
# Regression coverage for the scan.py/sentinel.py reader/writer divergence:
# scan.py used to derive its roots from bare Path.home(), ignoring CLAUDE_HOME
# entirely, while sentinel.py (the sibling writer) honoured it via
# _resolve_claude_home. See docs/decisions — DR-072 (settings-home dual-read)
# and machine-local-registry.md §4a (CLAUDE_HOME is a $HOME substitute, not
# the .claude dir itself).


def test_resolve_roots_honours_claude_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The regression that was broken today: CLAUDE_HOME must be honoured,
    not silently ignored in favor of bare Path.home()."""
    sandbox = tmp_path / "sandbox"
    monkeypatch.setenv("CLAUDE_HOME", str(sandbox))
    monkeypatch.delenv("COORDINATOR_PLUGINS_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_CONSUMER_HEALTH_ROOT", raising=False)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))

    plugins_root, consumer_root, sh_plugins_root, sh_consumer_root = _resolve_roots()

    assert plugins_root == sandbox / ".claude" / "plugins"
    assert consumer_root == sandbox / ".claude"
    assert sh_plugins_root == tmp_path / "settings-home" / "plugins"
    assert sh_consumer_root == tmp_path / "settings-home"


def test_resolve_roots_explicit_overrides_suppress_settings_home_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit COORDINATOR_PLUGINS_ROOT/COORDINATOR_CONSUMER_HEALTH_ROOT
    overrides replace their lane entirely — the settings-home mirror of that
    lane must be suppressed (None), not merely deprioritized, so the override
    wins over both the legacy AND settings-home candidates."""
    override_plugins = tmp_path / "override-plugins"
    override_consumer = tmp_path / "override-consumer"
    monkeypatch.setenv("COORDINATOR_PLUGINS_ROOT", str(override_plugins))
    monkeypatch.setenv("COORDINATOR_CONSUMER_HEALTH_ROOT", str(override_consumer))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))

    plugins_root, consumer_root, sh_plugins_root, sh_consumer_root = _resolve_roots()

    assert plugins_root == override_plugins
    assert consumer_root == override_consumer
    assert sh_plugins_root is None
    assert sh_consumer_root is None


def test_scan_verdicts_overrides_win_even_with_settings_home_collision(tmp_path: Path) -> None:
    """End-to-end confirmation of the override-wins rule at the scan_verdicts
    level: a REAL same-name sentinel is written under a real settings-home
    path on disk, but the caller passes sh_plugins_root=None/sh_consumer_root
    =None (as _resolve_roots produces under an explicit override) — proving
    the None-guard suppresses a genuine collision, not merely the absence of
    one. Without the guard (e.g. a bug that globbed sh_plugins_root
    regardless of the None-arg), the sh-hint AMBER line would win instead."""
    plugins_root = tmp_path / "override-plugins"
    consumer_root = tmp_path / "override-consumer"
    sh_plugins_root = tmp_path / "sh" / "plugins"
    _write_sentinel(
        plugins_root,
        "pluginOverride",
        {"verdict": "RED", "ran_at": "2020-01-01T00:00:00Z", "hint": "legacy-hint"},
    )
    _write_sentinel(
        sh_plugins_root,
        "pluginOverride",
        {"verdict": "AMBER", "ran_at": "2020-01-01T00:00:00Z", "hint": "sh-hint"},
    )

    lines = scan_verdicts(
        plugins_root, consumer_root, "--red-and-stale", 1893456000.0, 86400, None, None
    )

    assert len(lines) == 1
    assert "legacy-hint" in lines[0]
    assert "sh-hint" not in lines[0]
    assert "RED" in lines[0]


# ---------------------------------------------------------------------------
# scan_verdicts — settings-home dual-read lane (DR-072)
# ---------------------------------------------------------------------------


def test_scan_verdicts_discovers_settings_home_only_sentinel(tmp_path: Path) -> None:
    plugins_root = tmp_path / "claude-plugins"
    consumer_root = tmp_path / "claude-consumer"
    sh_plugins_root = tmp_path / "sh" / "plugins"
    sh_consumer_root = tmp_path / "sh"
    _write_sentinel(sh_plugins_root, "pluginSH", {"verdict": "RED", "ran_at": "2020-01-01T00:00:00Z"})

    lines = scan_verdicts(
        plugins_root, consumer_root, "--red-only", 1893456000.0, 86400, sh_plugins_root, sh_consumer_root
    )

    assert len(lines) == 1
    assert "pluginSH" in lines[0]


def test_scan_verdicts_settings_home_wins_on_same_plugin_collision(tmp_path: Path) -> None:
    """Settings-home wins UNCONDITIONALLY over the ~/.claude lane for the same
    plugin name — this is an authority-ordering rule, not a freshness/recency
    heuristic (DR-072: a ~/.claude copy is a disposable mirror, never
    authoritative)."""
    plugins_root = tmp_path / "claude-plugins"
    consumer_root = tmp_path / "claude-consumer"
    sh_plugins_root = tmp_path / "sh" / "plugins"
    sh_consumer_root = tmp_path / "sh"
    _write_sentinel(
        plugins_root,
        "pluginZ",
        {"verdict": "RED", "ran_at": "2020-01-01T00:00:00Z", "hint": "legacy-hint"},
    )
    _write_sentinel(
        sh_plugins_root,
        "pluginZ",
        {"verdict": "AMBER", "ran_at": "2020-01-01T00:00:00Z", "hint": "sh-hint"},
    )

    lines = scan_verdicts(
        plugins_root, consumer_root, "--red-and-stale", 1893456000.0, 86400, sh_plugins_root, sh_consumer_root
    )

    assert len(lines) == 1
    assert "sh-hint" in lines[0]
    assert "legacy-hint" not in lines[0]
    assert "AMBER" in lines[0]


def test_scan_verdicts_no_double_count_real_plugin(tmp_path: Path) -> None:
    """A real plugin present in only one lane must be counted exactly once,
    exercised against a genuinely POPULATED and OVERLAPPING settings-home
    lane (not an empty one — an empty sh lane cannot exercise any
    double-count or cross-leg-suppression bug at all). `sharedName` is
    written as a settings-home PLUGIN and, separately, as an unrelated
    legacy CONSUMER repo of the same bare name — the cross-leg collision
    named in Finding 4. Both must survive as distinct single-count entries,
    and the legacy-only `pluginOnlyLegacy` entry must be unaffected by the
    populated sh lane."""
    plugins_root = tmp_path / "claude-plugins"
    consumer_root = tmp_path / "claude-consumer"
    sh_plugins_root = tmp_path / "sh" / "plugins"
    sh_consumer_root = tmp_path / "sh"
    _write_sentinel(plugins_root, "pluginOnlyLegacy", {"verdict": "RED", "ran_at": "2020-01-01T00:00:00Z"})
    _write_sentinel(
        sh_plugins_root,
        "sharedName",
        {"verdict": "RED", "ran_at": "2020-01-01T00:00:00Z", "hint": "sh-plugin-hint"},
    )
    _write_sentinel(
        consumer_root,
        "sharedName",
        {"verdict": "RED", "ran_at": "2020-01-01T00:00:00Z", "hint": "legacy-consumer-hint"},
    )

    lines = scan_verdicts(
        plugins_root, consumer_root, "--red-and-stale", 1893456000.0, 86400, sh_plugins_root, sh_consumer_root
    )

    assert len(lines) == 3
    for needle in ("pluginOnlyLegacy", "sh-plugin-hint", "legacy-consumer-hint"):
        assert sum(1 for l in lines if needle in l) == 1
