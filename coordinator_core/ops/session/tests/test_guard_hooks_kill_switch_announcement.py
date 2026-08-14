"""
coordinator_core.ops.session.tests.test_guard_hooks_kill_switch_announcement

Tests for `guard_settings_integrity.evaluate_hooks_kill_switch_announcement`
(routine, `verbose=False`) /
`guard_settings_integrity.evaluate_hooks_kill_switch_full_detail` (on-demand,
`verbose=True`) / `_read_kill_switch_marker` / `_render_kill_switch_banner` --
the "make the kill-switch honest" detector added 2026-07-28/29, and the
route-vs-answer collapse of its not-expired case added 2026-07-30.

Four properties under test, matching the dispatch brief's own framing:
  1. Loud, always -- non-empty banner whenever the marker is PRESENT,
     regardless of expiry (silence-while-armed is the defect this closes).
     "Loud" no longer means "full detail" for the healthy not-expired case
     (see property 4) -- it means non-empty and unambiguous, at every
     verbosity.
  2. Expiry escalates, never disarms -- past `Expires:`, the banner gets
     louder and demands a human re-confirmation (hand-edit `Expires:`
     forward); this module structurally never writes to the marker.
  3. Malformed content (empty / undecodable / missing `Expires:`) fails
     loud and STAYS ARMED -- never degrades to "" (which would read as
     "quiet" / "nothing to see", indistinguishable from marker-absent).
  4. Route vs answer -- the not-expired ("healthy, armed") case collapses
     to ONE router line on the routine (boot) path, naming what's armed,
     since when, that it isn't expired yet, and the exact invocable
     command for the full detail. The full disarm-condition/double-fire
     body is NOT gone -- it is reachable via
     `evaluate_hooks_kill_switch_full_detail` / the
     `session.guard_hooks_kill_switch_detail` op, byte-identical to the
     pre-2026-07-30 routine output for that case. Malformed/expired are
     UNAFFECTED by this collapse -- both stay full and loud on the routine
     path too, because both are operator-actionable defects, not routine
     "everything's fine" status.

Negative-spec: this suite does NOT assert anything about repair/write
behavior, because there is none -- see the module's own "Kill-switch marker
loudness" section docstring. A future PR that adds a write path (auto-bump
`Expires:`, auto-delete on expiry, etc.) is a scope violation, not a bug fix.

Spec backlink: DoE-claude dispatch state/subagent-share/
78b683cd-1b62-4a25-904d-954cb3c69412/coordinatorexecutor-dcbed68d.md
(2026-07-28/29). Route-vs-answer collapse: state/handoffs/
2026-07-30-boot-context-bloat-non-orientation-surfaces.md item 4 / AC4.
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

from coordinator_core.ops.session import guard_settings_integrity as gsi


def _write_marker(config_dir, text: str):
    config_dir.mkdir(parents=True, exist_ok=True)
    marker = config_dir / gsi._KILL_SWITCH_MARKER_NAME
    marker.write_text(text, encoding="utf-8")
    return marker


# ---------------------------------------------------------------------------
# Quiet: marker absent.
# ---------------------------------------------------------------------------


def test_marker_absent_is_quiet(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    assert gsi.evaluate_hooks_kill_switch_announcement(config_dir) == ""


# ---------------------------------------------------------------------------
# Loud while armed, not yet expired -- property 1 + property 4 (route vs
# answer): the ROUTINE (boot) rendering collapses to one router line; the
# ON-DEMAND rendering keeps the full pre-2026-07-30 body.
# ---------------------------------------------------------------------------


def test_armed_not_expired_routine_is_one_router_line(tmp_path):
    config_dir = tmp_path / "config"
    future = (_dt.date.today() + _dt.timedelta(days=14)).isoformat()
    _write_marker(
        config_dir,
        f"Since: 2026-07-14\nExpires: {future}\nReason: Windows spawn tax.\n"
        "Disarm condition: delete once the naked-Python hook migration lands\n",
    )
    banner = gsi.evaluate_hooks_kill_switch_announcement(config_dir)
    assert banner != ""
    # Exactly one non-blank content line -- the router line, not the ~40-line
    # full body. Blank padding lines are permitted (join(["", line, ""])).
    content_lines = [ln for ln in banner.splitlines() if ln.strip()]
    assert len(content_lines) == 1, f"expected one router line, got: {content_lines!r}"
    assert "Coordinator hook generation is DISABLED" in banner
    assert "2026-07-14" in banner
    assert future in banner
    assert "not yet reached" in banner
    assert "session.guard_hooks_kill_switch_detail" in banner
    assert "python3 -m coordinator_core.invoke" in banner
    # The full body's disarm-condition/double-fire detail is NOT in the
    # routine router line -- that's the whole point of the collapse.
    assert "naked-Python hook migration lands" not in banner
    assert "MET as of 2026-07-28" not in banner
    assert "double-fire status" not in banner
    # Not yet the escalated past-expiry banner.
    assert "EXPIRED" not in banner
    assert "ACTION REQUIRED" not in banner


def test_armed_not_expired_full_detail_matches_pre_collapse_body(tmp_path):
    config_dir = tmp_path / "config"
    future = (_dt.date.today() + _dt.timedelta(days=14)).isoformat()
    _write_marker(
        config_dir,
        f"Since: 2026-07-14\nExpires: {future}\nReason: Windows spawn tax.\n"
        "Disarm condition: delete once the naked-Python hook migration lands\n",
    )
    detail = gsi.evaluate_hooks_kill_switch_full_detail(config_dir)
    assert detail != ""
    assert "Coordinator hook generation is DISABLED" in detail
    assert "Since:  2026-07-14" in detail
    assert future in detail
    assert "not yet reached" in detail
    assert "naked-Python hook migration lands" in detail
    assert "MET as of 2026-07-28" in detail
    assert "double-fire status" in detail
    # Not yet the escalated past-expiry banner.
    assert "EXPIRED" not in detail
    assert "ACTION REQUIRED" not in detail
    # Full detail is meaningfully longer than the routine router line.
    routine = gsi.evaluate_hooks_kill_switch_announcement(config_dir)
    assert len(detail) > len(routine)


# ---------------------------------------------------------------------------
# Escalated past expiry -- property 2 -- and it never disarms.
# ---------------------------------------------------------------------------


def test_expired_marker_escalates_and_stays_armed(tmp_path):
    config_dir = tmp_path / "config"
    past = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    marker = _write_marker(
        config_dir,
        f"Since: 2026-07-14\nExpires: {past}\nReason: Windows spawn tax.\n",
    )
    before = marker.read_bytes()

    banner = gsi.evaluate_hooks_kill_switch_announcement(config_dir)

    assert "EXPIRED and is" in banner
    assert "STILL ARMED" in banner
    assert "ACTION REQUIRED" in banner
    assert "does not disarm itself" in banner
    assert past in banner

    # Never disarms itself: the marker still exists, byte-identical, and a
    # second call still reports armed (not "" / not silently gone).
    assert marker.is_file()
    assert marker.read_bytes() == before
    assert gsi.evaluate_hooks_kill_switch_announcement(config_dir) != ""


def test_expiry_reached_today_counts_as_expired(tmp_path):
    """`>=`, not `>` -- the day Expires reads is itself already escalated,
    not one grace day."""
    config_dir = tmp_path / "config"
    today = _dt.date.today().isoformat()
    _write_marker(config_dir, f"Since: 2026-07-14\nExpires: {today}\n")
    banner = gsi.evaluate_hooks_kill_switch_announcement(config_dir)
    assert "ACTION REQUIRED" in banner


# ---------------------------------------------------------------------------
# Malformed content -- property 3 -- fails loud, never fails open.
# ---------------------------------------------------------------------------


def test_missing_expires_line_is_malformed_and_stays_armed(tmp_path):
    config_dir = tmp_path / "config"
    _write_marker(config_dir, "Coordinator hooks disabled. See the wiki.\n")
    banner = gsi.evaluate_hooks_kill_switch_announcement(config_dir)
    assert banner != ""
    assert "MALFORMED" in banner
    assert "ARMED" in banner
    assert "naked-Python hook migration lands" in banner  # still surfaced


def test_empty_marker_is_malformed_and_stays_armed(tmp_path):
    config_dir = tmp_path / "config"
    _write_marker(config_dir, "")
    banner = gsi.evaluate_hooks_kill_switch_announcement(config_dir)
    assert banner != ""
    assert "MALFORMED" in banner


def test_whitespace_only_marker_is_malformed(tmp_path):
    config_dir = tmp_path / "config"
    _write_marker(config_dir, "   \n\n\t\n")
    banner = gsi.evaluate_hooks_kill_switch_announcement(config_dir)
    assert banner != ""
    assert "MALFORMED" in banner


def test_unparseable_expires_value_is_malformed(tmp_path):
    config_dir = tmp_path / "config"
    _write_marker(config_dir, "Since: 2026-07-14\nExpires: not-a-date\n")
    banner = gsi.evaluate_hooks_kill_switch_announcement(config_dir)
    assert banner != ""
    assert "MALFORMED" in banner


def test_undecodable_marker_is_malformed_not_fatal(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    marker = config_dir / gsi._KILL_SWITCH_MARKER_NAME
    marker.write_bytes(b"\xff\xfe\x00\xff garbage bytes, not utf-8 \x80\x81")
    # Must never raise -- degrades to the malformed/fail-loud banner.
    banner = gsi.evaluate_hooks_kill_switch_announcement(config_dir)
    assert banner != ""
    assert "MALFORMED" in banner


# ---------------------------------------------------------------------------
# Double-fire status is folded into the FULL-DETAIL banner honestly (reuses
# the existing detector, never a second resolver). The routine router line
# never carries this detail -- it only names the command that does.
# ---------------------------------------------------------------------------


def _arm_double_fire_fixture(tmp_path, monkeypatch):
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    hooks_dir = content_root / "hooks"
    hooks_dir.mkdir()
    script = hooks_dir / "scripts" / "foo.py"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (hooks_dir / "hooks.json").write_text(
        '{"hooks": {"SessionStart": [{"hooks": '
        '[{"type": "command", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/foo.py"}]}]}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    config_dir = tmp_path / "config"
    future = (_dt.date.today() + _dt.timedelta(days=14)).isoformat()
    import json as _json

    _write_marker(config_dir, f"Since: 2026-07-14\nExpires: {future}\n")
    (config_dir / "settings.json").write_text(
        _json.dumps(
            {
                "enabledPlugins": {"foo@bar": True},
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {"type": "command", "command": f"python3 {script}"}
                            ]
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    return config_dir


def test_double_fire_summary_reflects_live_duplication_in_full_detail(tmp_path, monkeypatch):
    config_dir = _arm_double_fire_fixture(tmp_path, monkeypatch)
    detail = gsi.evaluate_hooks_kill_switch_full_detail(config_dir)
    assert "ALREADY firing twice right now" in detail
    assert "1 duplicated script(s)" in detail


def test_double_fire_summary_absent_from_routine_router_line(tmp_path, monkeypatch):
    config_dir = _arm_double_fire_fixture(tmp_path, monkeypatch)
    banner = gsi.evaluate_hooks_kill_switch_announcement(config_dir)
    assert "double-fire status" not in banner
    assert "ALREADY firing twice" not in banner
    # It still names the surface that WOULD show it.
    assert "session.guard_hooks_kill_switch_detail" in banner


# ---------------------------------------------------------------------------
# Never writes -- settings.json / hooks.json / the marker itself are
# byte-identical before and after every evaluate call, armed or expired.
# ---------------------------------------------------------------------------


def test_evaluate_never_writes_anything(tmp_path, monkeypatch):
    from coordinator_core.resolve_coordinator_clone import ResolveCoordinatorCloneError

    def _raise():
        raise ResolveCoordinatorCloneError("no coordinator content root on this (test) machine")

    monkeypatch.setattr(gsi, "resolve_content_root", _raise)

    config_dir = tmp_path / "config"
    past = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    marker = _write_marker(config_dir, f"Since: 2026-07-14\nExpires: {past}\n")
    settings_path = config_dir / "settings.json"
    settings_path.write_text('{"enabledPlugins": {"foo@bar": true}}', encoding="utf-8")

    marker_before = marker.read_bytes()
    settings_before = settings_path.read_bytes()

    gsi.evaluate_hooks_kill_switch_announcement(config_dir)
    gsi.evaluate_hooks_kill_switch_announcement(config_dir)

    assert marker.read_bytes() == marker_before
    assert settings_path.read_bytes() == settings_before


# ---------------------------------------------------------------------------
# The router line names an invocable surface -- prove it actually resolves,
# not merely that the string looks plausible. Runs the exact command
# `_KS_DETAIL_COMMAND` embeds (module var, not re-typed here) as a real
# subprocess against the `session.guard_hooks_kill_switch_detail` op,
# end-to-end through `coordinator_core.invoke`'s CLI dispatcher.
# ---------------------------------------------------------------------------


def test_router_line_command_actually_resolves(tmp_path):
    import json as _json
    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parents[4]
    assert (repo_root / "coordinator_core").is_dir(), (
        f"expected {repo_root} to be the coordinator_core repo root"
    )

    config_dir = tmp_path / "config"
    future = (_dt.date.today() + _dt.timedelta(days=14)).isoformat()
    _write_marker(config_dir, f"Since: 2026-07-14\nExpires: {future}\n")

    # The command _KS_DETAIL_COMMAND embeds, split into argv (no shell
    # parsing involved -- proves the op resolves, not that a shell string
    # happens to look right).
    assert gsi._KS_DETAIL_COMMAND == (
        "python3 -m coordinator_core.invoke session.guard_hooks_kill_switch_detail --bare"
    )
    argv = [
        sys.executable, "-m", "coordinator_core.invoke",
        "session.guard_hooks_kill_switch_detail", "--bare",
    ]
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    proc = subprocess.run(
        argv, cwd=str(repo_root), env=env, capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    # --bare prints the handler's bare result object (see
    # coordinator_core/invoke/__main__.py); this op's handler returns
    # {"text": <str>} (see _handler_kill_switch_detail's own docstring).
    result = _json.loads(proc.stdout)
    text = result["text"]
    assert "Coordinator hook generation is DISABLED" in text
    assert "double-fire status" in text  # full detail, not the router line
    assert "MET as of 2026-07-28" in text  # historical-disarm-condition line, full-detail-only
