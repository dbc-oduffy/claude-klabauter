"""Tests for coordinator_core.bash_guards._platform_verdict.

Coverage:
  (a) `platform_verdict_for_shape` is ADVISORY-ONLY (DR-280, 2026-08-07): it
      never returns a deny envelope, at any `host_is_windows` value
      (explicit True/False, omitted-and-sniffed, or declared-registry-
      overridden) -- always `permissionDecision: allow` +
      `additionalContext` naming the shape and the outlet, never a
      `permissionDecisionReason` key.
  (b) The low-level `platform_verdict` function is UNAFFECTED by DR-280 and
      still renders either envelope shape (deny/advise) from
      caller-supplied text, honoring the same override contract
      independent of the message-templating convenience wrapper above it.
  (c) `_resolve_host_is_windows` / `_sniff_host_is_windows` /
      `_declared_host_is_windows` platform-resolution plumbing (used by
      `platform_verdict`, and still exercised in isolation here) is
      unchanged by DR-280.

Pure Python -- no shell spawns, no real platform dependency.

Spec backlink: coordinator_core/bash_guards/_platform_verdict.py
Spec backlink (governing decision): docs/decisions/DR-280-unreachable-deny-legs-retire-rather-than.md
"""

from __future__ import annotations

import os
import sys

from coordinator_core.bash_guards import _platform_verdict as pv


def test_windows_override_still_advises_never_denies():
    # DR-280: platform_verdict_for_shape is advisory-only -- host_is_windows
    # no longer changes the envelope shape, even explicitly True.
    result = pv.platform_verdict_for_shape(
        "grep-via-bash",
        "grep -r foo .",
        "the search outlet",
        "search_outlet(query='foo')",
        host_is_windows=True,
    )
    out = result["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "allow"
    ctx = out["additionalContext"]
    assert "grep-via-bash" in ctx
    assert "the search outlet" in ctx
    assert "search_outlet(query='foo')" in ctx
    assert "permissionDecisionReason" not in out
    assert "DENIED" not in ctx


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


def test_shape_helper_allows_regardless_of_os_name(monkeypatch):
    # DR-280: platform_verdict_for_shape no longer varies by real host --
    # both os.name values render the same allow+advisory envelope.
    monkeypatch.setattr(pv, "_declared_host_is_windows", lambda: None)
    monkeypatch.setattr(os, "name", "nt")
    windows_result = pv.platform_verdict_for_shape(
        "for-loop-plumbing", "for f in *.txt; do cat $f; done", "the outlet", "outlet_call()"
    )
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    posix_result = pv.platform_verdict_for_shape(
        "for-loop-plumbing", "for f in *.txt; do cat $f; done", "the outlet", "outlet_call()"
    )
    assert windows_result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert posix_result["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_low_level_verdict_default_is_not_baked_in_at_import_time(monkeypatch):
    # Same process, same imported module object -- flipping os.name between
    # two calls must flip the LOW-LEVEL platform_verdict's verdict both
    # times, proving the read happens at call time (never cached on a prior
    # import or a prior call). platform_verdict_for_shape no longer varies
    # by host at all (DR-280), so this property is exercised against
    # `platform_verdict` directly instead.
    monkeypatch.setattr(pv, "_declared_host_is_windows", lambda: None)
    monkeypatch.setattr(os, "name", "nt")
    first = pv.platform_verdict("deny text", "advise text")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    second = pv.platform_verdict("deny text", "advise text")
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
    # sys.platform say. Exercised against the low-level `platform_verdict`
    # (via `_resolve_host_is_windows`, which both public functions share);
    # `platform_verdict_for_shape` itself no longer varies by this signal
    # (DR-280).
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(pv, "_declared_host_is_windows", lambda: True)
    assert pv._resolve_host_is_windows(None) is True

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(pv, "_declared_host_is_windows", lambda: False)
    assert pv._resolve_host_is_windows(None) is False


def test_shape_helper_ignores_host_is_windows_kwarg_entirely(monkeypatch):
    # DR-280: host_is_windows is vestigial on platform_verdict_for_shape --
    # still accepted (threaded by every live caller and dispatch.py's
    # registration lambdas) but has no effect on the rendered envelope.
    monkeypatch.setattr(pv, "_declared_host_is_windows", lambda: True)
    result = pv.platform_verdict_for_shape(
        "shape", "cmd", "outlet", "example", host_is_windows=False
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    result = pv.platform_verdict_for_shape(
        "shape", "cmd", "outlet", "example", host_is_windows=True
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


def test_shape_helper_advisory_does_not_echo_matched_cmd():
    # DR-280: platform_verdict_for_shape's advisory template never rendered
    # `matched_cmd` (only the retired deny message did); a pathological
    # long command costs nothing extra since it is accepted but unused.
    long_cmd = "grep -r " + ("x" * 400)
    result = pv.platform_verdict_for_shape(
        "grep-via-bash", long_cmd, "the outlet", "outlet_call()", host_is_windows=True
    )
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert long_cmd not in ctx


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
