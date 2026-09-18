"""coordinator_core/hooks/tests/test_arrival_w4_c10.py — the W4-C10 arrival
gate for the SessionStart family: fourteen hook ops ported from DoE-claude
per docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10.

Each op is exercised for its registration, its fail-open contract on a
missing/malformed payload, and at least one real-computation assertion drawn
from the DoE source script's own documented behavior — not a stub. Tests that
would require faking an entire filesystem/registry topology for a thin
plumbing op instead assert the fail-open shape directly (every op in this
row's own body is documented as unconditionally exit-0/no-advisory on any
resolution failure).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from coordinator_core.hooks import (
    assert_em_role,
    guard_hook_generation_self_probe,
    session_start_announce_job_mode,
    session_start_guard_plane_check,
    session_start_register_doe_claude_root,
    session_start_register_published_engine,
    session_start_repair_prepare_commit_msg_hook,
    session_start_watch_presence,
    session_start_write_plugin_root_breadcrumb,
    sessionstart_async_dispatch,
    sessionstart_bin_drift_refresh,
    sessionstart_dispatch,
    sessionstart_ensure_http_forwarder,
    sweep_boot,
)
from coordinator_core.ipc import _REGISTRY


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_EXPECTED_OP_NAMES = (
    "hooks.session_start_guard_plane_check",
    "hooks.session_start_announce_job_mode",
    "hooks.guard_hook_generation_self_probe",
    "hooks.session_start_register_doe_claude_root",
    "hooks.session_start_register_published_engine",
    "hooks.session_start_repair_prepare_commit_msg_hook",
    "hooks.session_start_write_plugin_root_breadcrumb",
    "hooks.sessionstart_bin_drift_refresh",
    "hooks.sessionstart_ensure_http_forwarder",
    "hooks.sweep_boot",
    "hooks.session_start_watch_presence",
    "hooks.assert_em_role",
    "hooks.sessionstart_dispatch",
    "hooks.sessionstart_async_dispatch",
)


@pytest.mark.parametrize("op_name", _EXPECTED_OP_NAMES)
def test_op_registered(op_name):
    assert op_name in _REGISTRY


# ---------------------------------------------------------------------------
# session_start_guard_plane_check — self-contained, real computation
# ---------------------------------------------------------------------------


def test_guard_plane_check_silent_outside_remote_session(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    assert session_start_guard_plane_check.build_report() is None


def test_guard_plane_check_counts_pretooluse_coordinator_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"command": "coordinator-guard-write.py"}]}
                    ],
                    "SessionStart": [
                        {"hooks": [{"command": "coordinator-unrelated.py"}]}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    assert session_start_guard_plane_check.count_coordinator_hooks(str(settings)) == 1


def test_guard_plane_check_absent_file_is_zero_not_error():
    assert session_start_guard_plane_check.count_coordinator_hooks("/nonexistent/x.json") == 0


def test_guard_plane_check_op_reports_absence(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    result = _run(session_start_guard_plane_check._handler({}))
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "NO registered coordinator hook" in context


# ---------------------------------------------------------------------------
# session_start_announce_job_mode
# ---------------------------------------------------------------------------


def test_announce_job_mode_reports_resolved_mode(monkeypatch):
    monkeypatch.setenv("COORDINATOR_JOB_MODE", "interactive")
    result = _run(
        session_start_announce_job_mode._handler({"payload": {"session_id": "sess-1"}})
    )
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "interactive" in context
    assert "asserted via COORDINATOR_JOB_MODE" in context


def test_announce_job_mode_fails_open_on_bad_payload():
    result = _run(session_start_announce_job_mode._handler({}))
    assert "hookSpecificOutput" in result  # resolves even with an absent payload


# ---------------------------------------------------------------------------
# guard_hook_generation_self_probe
# ---------------------------------------------------------------------------


def test_self_probe_fails_open_when_engine_module_unimportable(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name == "coordinator_core.ops.session.guard_hook_generation_self_probe":
            raise ImportError("blocked for test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    result = _run(guard_hook_generation_self_probe._handler({}))
    assert result == {}  # no_advisory()


def test_self_probe_returns_context_when_probe_emits_text(monkeypatch):
    import coordinator_core.ops.session.guard_hook_generation_self_probe as real_probe

    monkeypatch.setattr(real_probe, "run_self_probe", lambda config_dir=None: "banner text")
    result = _run(guard_hook_generation_self_probe._handler({}))
    assert "banner text" in result["hookSpecificOutput"]["additionalContext"]


# ---------------------------------------------------------------------------
# session_start_register_doe_claude_root — wrong-repo guard, real computation
# ---------------------------------------------------------------------------


def test_doe_claude_wrong_repo_guard_rejects_missing_sentinel(tmp_path):
    assert not session_start_register_doe_claude_root._is_genuine_doe_claude_repo(tmp_path)


def test_doe_claude_wrong_repo_guard_rejects_prefix_sharing_slug(tmp_path):
    (tmp_path / ".coordinator-dev-repo").write_text("slug: doe-claude-fork\n", encoding="utf-8")
    assert not session_start_register_doe_claude_root._is_genuine_doe_claude_repo(tmp_path)


def test_doe_claude_wrong_repo_guard_accepts_exact_slug(tmp_path):
    (tmp_path / ".coordinator-dev-repo").write_text("slug: doe-claude\n", encoding="utf-8")
    assert session_start_register_doe_claude_root._is_genuine_doe_claude_repo(tmp_path)


def test_doe_claude_root_handler_no_op_when_nothing_confirmed(tmp_path):
    result = _run(
        session_start_register_doe_claude_root._handler({"payload": {"cwd": str(tmp_path)}})
    )
    assert result == {}  # no_advisory() -- tmp_path carries no dev-repo sentinel


def test_doe_claude_root_handler_never_raises_on_absent_payload():
    result = _run(session_start_register_doe_claude_root._handler({}))
    assert result == {}


# ---------------------------------------------------------------------------
# session_start_register_published_engine
# ---------------------------------------------------------------------------


def test_is_stamped_engine_root_rejects_missing_stamp(tmp_path):
    (tmp_path / "coordinator_core").mkdir()
    assert not session_start_register_published_engine.is_stamped_engine_root(tmp_path)


def test_is_stamped_engine_root_accepts_nonempty_stamp(tmp_path):
    core = tmp_path / "coordinator_core"
    core.mkdir()
    (core / "_engine_stamp").write_bytes(b"stamped\n")
    assert session_start_register_published_engine.is_stamped_engine_root(tmp_path)


def test_published_engine_handler_never_raises():
    result = _run(session_start_register_published_engine._handler({}))
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# session_start_repair_prepare_commit_msg_hook
# ---------------------------------------------------------------------------


def test_repair_commit_msg_hook_no_op_outside_a_git_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _run(session_start_repair_prepare_commit_msg_hook._handler({}))
    assert isinstance(result, dict)


def test_repair_commit_msg_first_existing_finds_real_file(tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("x", encoding="utf-8")
    found = session_start_repair_prepare_commit_msg_hook._first_existing(
        [str(tmp_path / "missing.txt"), str(target)]
    )
    assert found == str(target)


# ---------------------------------------------------------------------------
# session_start_write_plugin_root_breadcrumb
# ---------------------------------------------------------------------------


def test_plugin_root_breadcrumb_no_op_without_plugin_root(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = _run(session_start_write_plugin_root_breadcrumb._handler({}))
    assert isinstance(result, dict)
    assert not (tmp_path / ".claude" / ".coordinator-plugin-root").exists()


def test_plugin_root_breadcrumb_writes_atomically(monkeypatch, tmp_path):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("HOME", str(tmp_path))
    _run(session_start_write_plugin_root_breadcrumb._handler({}))
    breadcrumb = tmp_path / ".claude" / ".coordinator-plugin-root"
    assert breadcrumb.read_text(encoding="utf-8").strip() == plugin_root.as_posix()


def test_plugin_root_breadcrumb_idempotent_no_rewrite(monkeypatch, tmp_path):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("HOME", str(tmp_path))
    _run(session_start_write_plugin_root_breadcrumb._handler({}))
    breadcrumb = tmp_path / ".claude" / ".coordinator-plugin-root"
    mtime_before = breadcrumb.stat().st_mtime_ns
    _run(session_start_write_plugin_root_breadcrumb._handler({}))
    assert breadcrumb.stat().st_mtime_ns == mtime_before


# ---------------------------------------------------------------------------
# sessionstart_bin_drift_refresh
# ---------------------------------------------------------------------------


def test_bin_drift_refresh_fails_open_on_error(monkeypatch):
    def _raise(bin_dir):
        raise RuntimeError("boom")

    monkeypatch.setattr(sessionstart_bin_drift_refresh, "check_and_refresh", _raise)
    result = _run(sessionstart_bin_drift_refresh._handler({}))
    assert result == {}


def test_bin_drift_refresh_returns_banner(monkeypatch):
    monkeypatch.setattr(
        sessionstart_bin_drift_refresh, "check_and_refresh", lambda bin_dir: "refreshed x"
    )
    result = _run(sessionstart_bin_drift_refresh._handler({}))
    assert "refreshed x" in result["hookSpecificOutput"]["additionalContext"]


# ---------------------------------------------------------------------------
# sessionstart_ensure_http_forwarder
# ---------------------------------------------------------------------------


def test_ensure_http_forwarder_no_op_without_plugin_root(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    result = _run(sessionstart_ensure_http_forwarder._handler({}))
    assert result == {}


def test_probe_bind_wins_or_loses_cleanly():
    result = sessionstart_ensure_http_forwarder._probe_bind_wins(port=0)
    # port=0 asks the OS to pick an ephemeral port -- always wins, never
    # address-in-use; exercises the real socket path without a fixed port.
    assert result is True


def test_forwarder_argv_regex_matches_whole_path_component():
    rx = sessionstart_ensure_http_forwarder._FORWARDER_ARGV_RE
    assert rx.search("python3 /repo/hooks/http_hook_forwarder.py")
    assert not rx.search("pytest test_http_hook_forwarder_staleness.py")
    assert not rx.search("edit http_hook_forwarder_decoy.py")


# ---------------------------------------------------------------------------
# sweep_boot
# ---------------------------------------------------------------------------


def test_sweep_boot_session_reap_due_true_when_no_marker(tmp_path):
    assert sweep_boot._session_reap_due(str(tmp_path)) is True


def test_sweep_boot_session_reap_due_false_when_marker_fresh(tmp_path):
    marker_dir = tmp_path / ".git" / "coordinator-sessions"
    marker_dir.mkdir(parents=True)
    (marker_dir / ".last-reap").write_text("", encoding="utf-8")
    assert sweep_boot._session_reap_due(str(tmp_path)) is False


def test_sweep_boot_cache_head_parses_frontmatter(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "orientation_cache.md").write_text(
        "---\ngit_head_at_generation: abc123\n---\nbody\n", encoding="utf-8"
    )
    assert sweep_boot._read_cache_head(str(tmp_path)) == "abc123"


def test_sweep_boot_cache_head_none_when_missing():
    assert sweep_boot._read_cache_head("/nonexistent/repo") is None


def test_sweep_boot_handler_never_raises(monkeypatch):
    monkeypatch.setenv("COORDINATOR_ORIENTATION_SELFHEAL_OFF", "1")
    monkeypatch.setenv("COORDINATOR_SESSION_REAP_OFF", "1")
    result = _run(sweep_boot._handler({}))
    assert result == {}


# ---------------------------------------------------------------------------
# session_start_watch_presence
# ---------------------------------------------------------------------------


def test_watch_presence_render_presence_line_none_when_no_holder():
    assert session_start_watch_presence.render_presence_line(None) is None
    assert session_start_watch_presence.render_presence_line({}) is None


def test_watch_presence_render_presence_line_named_holder():
    line = session_start_watch_presence.render_presence_line({"holder_name": "Riker"})
    assert "Riker" in line


def test_watch_presence_handler_no_op_today(monkeypatch):
    result = _run(session_start_watch_presence._handler({}))
    assert result == {}  # no_advisory() -- no watch_heartbeat/uhura-mode module yet


# ---------------------------------------------------------------------------
# assert_em_role
# ---------------------------------------------------------------------------


def test_assert_em_role_missing_plugin_root_still_returns_envelope(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    result = _run(assert_em_role._handler({"payload": {}}))
    assert "hookSpecificOutput" in result
    assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_assert_em_role_delivers_plugin_snippet(monkeypatch, tmp_path):
    plugin_root = tmp_path / "plugin"
    (plugin_root / "snippets").mkdir(parents=True)
    (plugin_root / "snippets" / "agent-role-em.md").write_text(
        "You are the EM.", encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    result = _run(assert_em_role._handler({"payload": {"cwd": str(tmp_path)}}))
    assert "You are the EM." in result["hookSpecificOutput"]["additionalContext"]


def test_assert_em_role_missing_plugin_snippet_banners(monkeypatch, tmp_path):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    result = _run(assert_em_role._handler({"payload": {"cwd": str(tmp_path)}}))
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "agent-role-em.md MISSING" in context


def test_assert_em_role_repo_slot_is_silent_when_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    result = _run(assert_em_role._handler({"payload": {"cwd": str(tmp_path)}}))
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "em-context.md" not in context


# ---------------------------------------------------------------------------
# sessionstart_dispatch / sessionstart_async_dispatch — fan-in aggregation
# ---------------------------------------------------------------------------


def test_sessionstart_dispatch_concatenates_leg_output(monkeypatch):
    monkeypatch.setenv("COORDINATOR_JOB_MODE", "cron")
    monkeypatch.setattr(
        sessionstart_dispatch,
        "_sessionstart_bin_drift_refresh_handler",
        lambda params: _identity_no_advisory(),
    )
    monkeypatch.setattr(
        sessionstart_dispatch,
        "_guard_hook_generation_self_probe_handler",
        lambda params: _identity_no_advisory(),
    )
    result = _run(sessionstart_dispatch._handler({"payload": {"session_id": "s1"}}))
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "cron" in context


def test_sessionstart_dispatch_one_leg_failure_does_not_drop_others(monkeypatch):
    async def _raising(params):
        raise RuntimeError("leg failure")

    monkeypatch.setattr(sessionstart_dispatch, "_sessionstart_bin_drift_refresh_handler", _raising)
    monkeypatch.setattr(
        sessionstart_dispatch,
        "_guard_hook_generation_self_probe_handler",
        lambda params: _identity_no_advisory(),
    )
    monkeypatch.setenv("COORDINATOR_JOB_MODE", "blitz")
    result = _run(sessionstart_dispatch._handler({"payload": {}}))
    assert "blitz" in result["hookSpecificOutput"]["additionalContext"]


def test_sessionstart_async_dispatch_never_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    result = _run(sessionstart_async_dispatch._handler({"payload": {"cwd": str(tmp_path)}}))
    assert isinstance(result, dict)


async def _identity_no_advisory():
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": None}}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
