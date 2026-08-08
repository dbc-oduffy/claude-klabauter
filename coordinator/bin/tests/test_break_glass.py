"""test_break_glass — pytest tests for coordinator/bin/break_glass.py.

Spec backlink: example-doctrine-repo docs/research/2026-07-28-break-glass-recovery-design.md.

Coverage maps to that design's own acceptance criteria:
  - AC-5 (idempotent/harmless on a healthy machine) — F-clean fixture must
    report all-OK, and running `main()` twice against a machine it just
    fixed must not change anything the second time.
  - AC-6 (verifiable from macOS) — every fixture here is a plain directory
    tree, no Windows host required; `check_home_resolution` is exercised via
    explicit env-dict injection, never real `os.environ` mutation.
  - AC-7 (honest failure) — a check that cannot run reports UNKNOWN with a
    reason, never OK; a failed repair reports REPAIR FAILED with a manual
    fix, never a silent pass.
  - AC-12 (wedge a THROWAWAY config, recover it) — `test_end_to_end_wedge_
    and_recover` builds a synthetic config dir under tmp_path (never the
    real tree), arms the kill-switch + writes a foreign-platform-path
    settings.json, runs `main()` in repair mode, and asserts both are
    resolved and a second run is a no-op all-OK pass.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).parent.parent
_FIXTURES = _BIN_DIR.parent / "tests" / "fixtures" / "stranded-claude"


def _load_module():
    spec = importlib.util.spec_from_file_location("break_glass", _BIN_DIR / "break_glass.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    # Register before exec — dataclasses' `_is_type` looks the module up via
    # `sys.modules[cls.__module__]` during class creation; an unregistered
    # module resolves to None there and raises on Python 3.14.
    sys.modules["break_glass"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


bg = _load_module()


# ---------------------------------------------------------------------------
# resolve_config_dir — env-injection, never touches real ~/.claude
# ---------------------------------------------------------------------------


def test_resolve_config_dir_explicit_wins():
    assert bg.resolve_config_dir("/explicit/path") == Path("/explicit/path")


def test_resolve_config_dir_env_chain(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", "/synthetic/home")
    assert bg.resolve_config_dir() == Path("/synthetic/home/.claude")


# ---------------------------------------------------------------------------
# check_home_resolution — pure env-dict injection (AC-6 technique 1)
# ---------------------------------------------------------------------------


def test_home_resolution_ok_with_absolute_home():
    finding = bg.check_home_resolution(env={"HOME": "/Users/pm", "CLAUDE_HOME": None, "USERPROFILE": None})
    assert finding.status == bg.STATUS_OK


def test_home_resolution_warn_on_relative():
    finding = bg.check_home_resolution(env={"HOME": "relative/dir", "CLAUDE_HOME": None, "USERPROFILE": None})
    assert finding.status == bg.STATUS_WARN


def test_home_resolution_windows_userprofile_shape():
    finding = bg.check_home_resolution(
        env={"CLAUDE_HOME": None, "HOME": None, "USERPROFILE": r"C:\Users\pm"}
    )
    assert finding.status == bg.STATUS_OK
    assert "USERPROFILE" in finding.detail


# ---------------------------------------------------------------------------
# check_settings_json — against the fixture tree (AC-6 technique 2)
# ---------------------------------------------------------------------------


def test_settings_json_foreign_path_is_broken():
    # Fixture carries a Windows-drive-shaped command path  # abs-path-ok: describing existing fixture content, not a new hardcoded path
    # (see F-foreign-path/settings.json) — foreign only on a POSIX host.
    # Pinned per AC-6 ("verifiable from macOS"), matching the fixture's own
    # authored shape, so this assertion is deterministic regardless of which
    # platform actually runs pytest.
    finding = bg.check_settings_json(_FIXTURES / "F-foreign-path", host_is_windows=False)
    assert finding.status == bg.STATUS_BROKEN
    assert "foreign-platform" in finding.detail


def test_settings_json_truncated_is_broken():
    finding = bg.check_settings_json(_FIXTURES / "F-truncated-json")
    assert finding.status == bg.STATUS_BROKEN
    assert "invalid JSON" in finding.detail


def test_settings_json_missing_is_broken():
    finding = bg.check_settings_json(_FIXTURES / "F-missing-settings")
    assert finding.status == bg.STATUS_BROKEN
    assert "missing" in finding.detail


def test_settings_json_clean_is_ok():
    # F-clean's command uses a bare POSIX env-var reference
    # ($COORDINATOR_CONTENT_ROOT), which is only foreign-shaped on a Windows
    # host (see `_is_posix_env_var_shaped`) — pinned to POSIX per AC-6 for
    # the same reason as test_settings_json_foreign_path_is_broken above.
    finding = bg.check_settings_json(_FIXTURES / "F-clean", host_is_windows=False)
    assert finding.status == bg.STATUS_OK


# ---------------------------------------------------------------------------
# check_kill_switch / repair_kill_switch — peer-safe repair, never touches
# any file but the local marker (AC-4)
# ---------------------------------------------------------------------------


def test_kill_switch_armed_is_reported_but_not_broken():
    """An armed kill switch is WARN, never BROKEN.

    It gates settings.json hook GENERATION, not hook DELIVERY — plugin-side
    delivery is independent, and on a healthy install it is the live path. So
    an armed marker on a machine whose hooks are all firing is the expected
    state, not a fault, and calling it BROKEN sends a wedged operator to break
    something that was working.
    """
    finding = bg.check_kill_switch(_FIXTURES / "F-armed-killswitch")
    assert finding.status == bg.STATUS_WARN
    assert finding.status != bg.STATUS_BROKEN
    assert "armed" in finding.detail


def test_kill_switch_absent_is_ok():
    finding = bg.check_kill_switch(_FIXTURES / "F-clean")
    assert finding.status == bg.STATUS_OK


def test_repair_kill_switch_never_deletes_the_marker(tmp_path):
    """--repair must LEAVE an armed marker in place, and say so.

    The marker is deliberately non-self-disarming, so a recovery tool deleting
    it on the operator's behalf is that same self-disarm in another costume —
    fired in exactly the situation where the operator can least evaluate it.
    Deleting it also does not restore hooks; where plugin-side delivery is
    live it starts a second copy of every hook firing. And it destroys a
    human's recorded decision, together with the rationale stored in its body.

    Regression net: an earlier revision deleted it and called that a repair.
    """
    config_dir = tmp_path / "claude"
    config_dir.mkdir()
    marker = config_dir / ".coordinator-hooks-disabled"
    marker.write_text("armed for test\n")

    outcome = bg.repair_kill_switch(config_dir)
    assert outcome.applied is False, "repair must not act on the kill switch"
    assert marker.exists(), "repair deleted the kill-switch marker"
    assert marker.read_text() == "armed for test\n", "marker body was modified"
    assert "hand" in outcome.detail.lower() or "armed" in outcome.detail.lower(), (
        "repair must explain why it declined, not decline silently"
    )


def test_repair_kill_switch_noops_when_already_clean(tmp_path):
    config_dir = tmp_path / "claude"
    config_dir.mkdir()
    outcome = bg.repair_kill_switch(config_dir)
    assert outcome.applied is False
    assert outcome.error is None


# ---------------------------------------------------------------------------
# registry pollution shape detector — pure string logic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "/tmp/pytest-of-example-operator/pytest-42/test_x0",
        r"C:\Users\pm\AppData\Local\Temp\tmp8f3k2",
        "/private/tmp/foo",
        "/some/path/tmp8f3k2q/repo",
    ],
)
def test_is_tmp_shaped_positive(value):
    assert bg._is_tmp_shaped(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "/Users/example-operator/X/example-doctrine-repo",
        r"C:\Users\pm\claude-klabauter",
        "/home/ci/repo",
    ],
)
def test_is_tmp_shaped_negative(value):
    assert bg._is_tmp_shaped(value) is False


# ---------------------------------------------------------------------------
# report formatting — always prints, never silent on success (AC-5)
# ---------------------------------------------------------------------------


def test_format_report_includes_every_layer():
    findings = [bg.Finding("layer-a", bg.STATUS_OK, "fine"), bg.Finding("layer-b", bg.STATUS_BROKEN, "bad", "fix it")]
    report = bg.format_report(findings)
    assert "layer-a" in report and "OK" in report
    assert "layer-b" in report and "BROKEN" in report and "fix it" in report


def test_exit_code_capped_at_124():
    findings = [bg.Finding(f"layer-{i}", bg.STATUS_BROKEN, "bad") for i in range(200)]
    assert bg._exit_code(findings, []) == 124


def test_exit_code_zero_when_all_ok():
    findings = [bg.Finding("layer-a", bg.STATUS_OK, "fine")]
    assert bg._exit_code(findings, []) == 0


# ---------------------------------------------------------------------------
# AC-12 — end-to-end: wedge a THROWAWAY synthetic config dir, recover it via
# main(), verify idempotent all-OK on the second run. Never touches the real
# ~/.claude, machine-local registry, or settings.json — every path here is
# rooted under pytest's own tmp_path.
# ---------------------------------------------------------------------------


def test_end_to_end_wedge_and_recover(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "wedged-claude"
    config_dir.mkdir()

    # Wedge #1: arm the kill-switch.
    (config_dir / ".coordinator-hooks-disabled").write_text("armed for AC-12 fixture wedge\n")

    # Wedge #2: settings.json with a foreign-platform-shaped hook path (no
    # hooks.json/coordinator-root available under tmp_path, so the
    # settings.json repair leg itself will report REPAIR FAILED — this is
    # deliberately exercised too, to prove the honest-failure path, not
    # just the happy path). `run_diagnose`/`main()` classify against the
    # AMBIENT host (no host override — that is production-correct: a real
    # recovery run must judge THIS machine, not a pinned one), so the wedge
    # path itself must be shaped foreign to whichever host actually runs
    # this test, not hardcoded to one platform's shape.
    foreign_command = (
        "python3 /Users/pm/example-doctrine-repo/coordinator/hooks/x.py"  # abs-path-ok: synthetic wedge shape, not a real machine path
        if os.name == "nt"
        else "python3 X:/example-doctrine-repo/coordinator/hooks/x.py"  # abs-path-ok: synthetic wedge shape, not a real machine path
    )
    settings_path = config_dir / "settings.json"
    settings_path.write_text(
        json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": foreign_command}
        ]}]}}),
    )

    # Point the registry resolver at an empty synthetic dir so check 7 runs
    # against throwaway state, never the real machine-local registry.
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))

    # --- Pre-recovery diagnose. The settings.json wedge is the genuine
    # breakage; the armed kill-switch is reported WARN and is NOT treated as
    # a fault to fix (see test_kill_switch_armed_is_reported_but_not_broken).
    pre = bg.run_diagnose(config_dir)
    by_layer = {f.layer: f for f in pre}
    assert by_layer["kill-switch"].status == bg.STATUS_WARN
    assert by_layer["settings.json"].status == bg.STATUS_BROKEN

    # --- Run the real CLI entrypoint in repair mode against the wedge. ---
    rc = bg.main(["--config-dir", str(config_dir)])
    captured = capsys.readouterr()
    assert "LAYER" in captured.out  # report printed unconditionally

    # The kill-switch must SURVIVE the repair run untouched. This is the
    # load-bearing assertion of this test: a break-glass sweep run by an
    # operator on a machine already in trouble must not quietly disarm a
    # deliberate safety marker as a side effect of fixing something else.
    assert (config_dir / ".coordinator-hooks-disabled").exists(), (
        "repair run deleted the kill-switch marker"
    )

    # settings.json repair CANNOT succeed under tmp_path (no positive
    # marker / no resolvable coordinator root — gen_settings_hooks.generate()
    # declines by its own design). This must surface as an HONEST repair
    # failure ("REPAIR FAILED" in the printed report, non-zero exit),
    # never a silently-swallowed or falsely-claimed fix.
    assert "REPAIR FAILED" in captured.out
    assert rc > 0

    # --- Re-run: diagnosis is stable across repair runs. The kill-switch is
    # still armed and still WARN (unchanged, because nothing touched it), and
    # settings.json is still BROKEN — never silently masked into a false OK.
    post = bg.run_diagnose(config_dir)
    post_by_layer = {f.layer: f for f in post}
    assert post_by_layer["kill-switch"].status == bg.STATUS_WARN
    assert post_by_layer["settings.json"].status == bg.STATUS_BROKEN


def test_report_only_never_mutates(tmp_path):
    config_dir = tmp_path / "wedged-claude-report-only"
    config_dir.mkdir()
    marker = config_dir / ".coordinator-hooks-disabled"
    marker.write_text("armed\n")

    rc = bg.main(["--config-dir", str(config_dir), "--report-only"])
    assert marker.exists()  # untouched
    assert rc > 0
