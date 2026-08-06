"""Tests for coordinator_core.bash_guards._platform_verdict.

Coverage:
  (a) host_is_windows=True  -> deny envelope (permissionDecision: deny),
      reason names the shape, the truncated command, and the outlet.
  (b) host_is_windows=False -> allow_advisory envelope (permissionDecision:
      allow + additionalContext), context names the shape and the outlet,
      never a deny key.
  (c) host_is_windows omitted -> tracks `os.name` at CALL time (monkeypatched
      both ways from a single macOS test process), proving the default is
      not baked in at import time and cannot be influenced by any
      environment variable (AC-8 discharge).
  (d) `platform_verdict` (the low-level function) honors the same override
      independent of the message-templating convenience wrapper.
  (e) a long matched command is truncated in the deny path (mirrors
      `block_worktree_creation._deny_reason`'s truncation convention).

Pure Python -- no shell spawns, no real platform dependency.

Spec backlink: coordinator_core/bash_guards/_platform_verdict.py
"""

from __future__ import annotations

import os
import sys

from coordinator_core.bash_guards import _platform_verdict as pv


def test_windows_override_denies():
    result = pv.platform_verdict_for_shape(
        "grep-via-bash",
        "grep -r foo .",
        "the search outlet",
        "search_outlet(query='foo')",
        host_is_windows=True,
    )
    out = result["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    assert "grep-via-bash" in reason
    assert "grep -r foo ." in reason
    assert "the search outlet" in reason
    assert "search_outlet(query='foo')" in reason
    assert "additionalContext" not in out


def test_macos_override_advises_never_denies():
    result = pv.platform_verdict_for_shape(
        "grep-via-bash",
        "grep -r foo .",
        "the search outlet",
        "search_outlet(query='foo')",
        host_is_windows=False,
    )
    out = result["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "allow"
    ctx = out["additionalContext"]
    assert "grep-via-bash" in ctx
    assert "the search outlet" in ctx
    assert "search_outlet(query='foo')" in ctx
    assert "permissionDecisionReason" not in out


def test_default_tracks_os_name_windows(monkeypatch):
    monkeypatch.setattr(pv, "_declared_host_is_windows", lambda: None)
    monkeypatch.setattr(os, "name", "nt")
    result = pv.platform_verdict_for_shape(
        "for-loop-plumbing", "for f in *.txt; do cat $f; done", "the outlet", "outlet_call()"
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_default_tracks_os_name_posix(monkeypatch):
    monkeypatch.setattr(pv, "_declared_host_is_windows", lambda: None)
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    result = pv.platform_verdict_for_shape(
        "for-loop-plumbing", "for f in *.txt; do cat $f; done", "the outlet", "outlet_call()"
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_default_is_not_baked_in_at_import_time(monkeypatch):
    # Same process, same imported module object -- flipping os.name between
    # two calls must flip the verdict both times, proving the read happens
    # at call time (never cached on a prior import or a prior call).
    monkeypatch.setattr(pv, "_declared_host_is_windows", lambda: None)
    monkeypatch.setattr(os, "name", "nt")
    first = pv.platform_verdict_for_shape("shape", "cmd", "outlet", "example")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    second = pv.platform_verdict_for_shape("shape", "cmd", "outlet", "example")
    assert first["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert second["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_sniff_widens_to_cygwin_and_msys_sys_platform(monkeypatch):
    # Git-for-Windows' bundled MSYS2/Cygwin Python interpreter can report
    # os.name == "posix" despite running on a Windows host -- sys.platform
    # is the corroborating signal that still catches it.
    monkeypatch.setattr(pv, "_declared_host_is_windows", lambda: None)
    monkeypatch.setattr(os, "name", "posix")
    for plat in ("cygwin", "msys"):
        monkeypatch.setattr(sys, "platform", plat)
        assert pv._sniff_host_is_windows() is True
    monkeypatch.setattr(sys, "platform", "linux")
    assert pv._sniff_host_is_windows() is False


def test_declared_registry_value_wins_over_sniffing(monkeypatch):
    # A declared registry value is the operator's authoritative correction
    # for a misdetecting box -- it must win regardless of what os.name/
    # sys.platform say.
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(pv, "_declared_host_is_windows", lambda: True)
    result = pv.platform_verdict_for_shape("shape", "cmd", "outlet", "example")
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(pv, "_declared_host_is_windows", lambda: False)
    result = pv.platform_verdict_for_shape("shape", "cmd", "outlet", "example")
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_explicit_override_kwarg_still_wins_over_declared_registry_value(monkeypatch):
    monkeypatch.setattr(pv, "_declared_host_is_windows", lambda: True)
    result = pv.platform_verdict_for_shape(
        "shape", "cmd", "outlet", "example", host_is_windows=False
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_declared_registry_value_parsing(monkeypatch):
    for raw, expected in (
        ("true", True), ("True", True), ("1", True), ("yes", True),
        ("false", False), ("False", False), ("0", False), ("no", False),
        ("garbage", None), ("", None),
    ):
        monkeypatch.setattr(pv, "_read_declared_registry_value", lambda _v=raw: _v)
        assert pv._declared_host_is_windows() == expected


def test_absent_registry_value_falls_through_to_sniffing_not_false(monkeypatch):
    # Absence must mean "consult sniffing", never "not Windows" outright.
    monkeypatch.setattr(pv, "_read_declared_registry_value", lambda: None)
    monkeypatch.setattr(os, "name", "nt")
    assert pv._resolve_host_is_windows(None) is True


def test_declared_registry_read_is_fresh_not_cached(monkeypatch):
    # No caching in this module -- flipping the underlying registry read
    # return value between calls must flip the verdict both times.
    monkeypatch.setattr(pv, "_read_declared_registry_value", lambda: "true")
    assert pv._resolve_host_is_windows(None) is True
    monkeypatch.setattr(pv, "_read_declared_registry_value", lambda: "false")
    assert pv._resolve_host_is_windows(None) is False


def test_low_level_platform_verdict_honors_override():
    denied = pv.platform_verdict("deny text", "advise text", host_is_windows=True)
    advised = pv.platform_verdict("deny text", "advise text", host_is_windows=False)
    assert denied["hookSpecificOutput"]["permissionDecisionReason"].endswith("deny text")
    assert advised["hookSpecificOutput"]["additionalContext"].endswith("advise text")


def test_long_command_truncated_in_deny_message():
    long_cmd = "grep -r " + ("x" * 400)
    result = pv.platform_verdict_for_shape(
        "grep-via-bash", long_cmd, "the outlet", "outlet_call()", host_is_windows=True
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert long_cmd not in reason
    assert "..." in reason


def test_no_override_env_var_influences_anything(monkeypatch):
    # AC-8: nothing an agent can set via settings.json's `env` block may
    # reach this decision. Setting every plausible override-shaped env var
    # must have zero effect on the verdict.
    monkeypatch.setattr(pv, "_declared_host_is_windows", lambda: None)
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("COORDINATOR_OVERRIDE_PLATFORM_VERDICT", "1")
    monkeypatch.setenv("COORDINATOR_OVERRIDE_BASH_SPAWN", "1")
    result = pv.platform_verdict_for_shape("shape", "cmd", "outlet", "example")
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_declared_registry_key_env_override_does_not_influence_verdict(monkeypatch, tmp_path):
    # Review: code-reviewer (F1, P1) -- `machine_resolver.registry_get` has
    # its own MACHINE_LOCAL_<KEY> env-override rung, checked BEFORE the TOML
    # file, which is a live third override surface for this specific
    # security-sensitive key unless `_declared_host_is_windows` reads the
    # TOML directly instead of going through `registry_get`. This is real,
    # not a real host on this machine confirms it: with the pre-fix code,
    # MACHINE_LOCAL_COORDINATOR_HOST_IS_WINDOWS=false silently downgrades
    # every PLATFORM_CONDITIONED_DENY guard from DENY to ADVISE on a genuine
    # Windows box. Uses a real empty registry dir (no declared value) so the
    # only signal in play is the env var; asserts sniffing (os.name) alone
    # decides the verdict in both directions, in both env-var polarities.
    monkeypatch.setattr(
        "coordinator_core.machine_resolver.registry_dir", lambda: tmp_path
    )
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(sys, "platform", "win32")

    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_HOST_IS_WINDOWS", "false")
    assert pv._resolve_host_is_windows(None) is True  # real host (nt) must win, not the env var

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_HOST_IS_WINDOWS", "true")
    assert pv._resolve_host_is_windows(None) is False  # real host (posix) must win, not the env var
