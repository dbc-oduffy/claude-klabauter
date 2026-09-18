"""coordinator_core/hooks/tests/test_arrival_w4_c13.py — the W4-C13 arrival
gate for `coordinator_core.hooks.runtime_tripwire_em_check` (already-landed
PostToolUse(Agent) warm-door op, reconciled/confirmed by this chunk) and
`coordinator_core.hooks.runtime_tripwire_stop_watcher` (new landing, this
chunk's own write — a stood-down, unregistered CLI-shaped port; see that
module's own docstring for why it carries no `register_op`).

Subject: docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W4-C13.

`runtime_tripwire_em_check` coverage lives in the already-landed
`test_runtime_tripwire_em_check.py` (op registration/classification/
behavior) — this file does not duplicate it, only confirms this chunk's own
reconciliation left that module's op contract intact.

`runtime_tripwire_stop_watcher` coverage here is ported behavior parity
against DoE-claude's own synchronous, disk-observable invariants (module's
own docstring: "loop guard, single-instance PID lock, dispatch-file scan,
threshold computation, wake-condition recheck, lock cleanup ... fully
verifiable and ARE verified"), plus the not-an-op / stood-down shape
assertions this port's own docstring commits to.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from coordinator_core.hooks import runtime_tripwire_stop_watcher as sw


# ---------------------------------------------------------------------------
# runtime_tripwire_em_check reconciliation — the op contract this chunk's
# writes: list re-touches must stay intact.
# ---------------------------------------------------------------------------


def test_em_check_op_still_registers_and_resolves() -> None:
    import importlib

    module = importlib.import_module("coordinator_core.hooks.runtime_tripwire_em_check")
    assert hasattr(module, "_handler")

    from coordinator_core.ipc import _REGISTRY

    assert "hooks.runtime_tripwire_em_check" in _REGISTRY


# ---------------------------------------------------------------------------
# runtime_tripwire_stop_watcher — stood-down / not-an-op shape.
# ---------------------------------------------------------------------------


def test_stop_watcher_module_registers_no_op():
    """No `register_op` call at import time -- this module keeps its
    original CLI shape (`main()` / `--watch` re-exec), per its own
    module-docstring rationale. A stray `register_op` here would silently
    create a `hooks.runtime_tripwire_stop_watcher` op nothing in DoE-claude's
    own `hooks.json` (stood-down roster) ever calls."""
    assert not hasattr(sw, "register_op")


def test_stop_watcher_module_is_not_in_eager_hook_modules():
    from coordinator_core.hooks import _EAGER_HOOK_MODULES

    assert "coordinator_core.hooks.runtime_tripwire_stop_watcher" not in _EAGER_HOOK_MODULES


def test_stop_watcher_import_is_fast():
    mod_name = "coordinator_core.hooks.runtime_tripwire_stop_watcher"
    sys.modules.pop(mod_name, None)
    start = time.perf_counter()
    import coordinator_core.hooks.runtime_tripwire_stop_watcher  # noqa: F401

    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"import took {elapsed:.3f}s, over the 500ms brightline"


# ---------------------------------------------------------------------------
# runtime_threshold_minutes — per-model ceiling, byte-faithful port.
# ---------------------------------------------------------------------------


def test_threshold_opus_default():
    assert sw.runtime_threshold_minutes("claude-opus-4") == 25


def test_threshold_sonnet():
    assert sw.runtime_threshold_minutes("claude-sonnet-4-5") == 12


def test_threshold_haiku():
    assert sw.runtime_threshold_minutes("claude-haiku-4-5") == 10


def test_threshold_1m_context_variant_uses_opus_default():
    """Explicit 1M-context arm matched BEFORE the bare sonnet/opus arms --
    negative spec: a 1M-context Sonnet must not misclassify as plain
    Sonnet's smaller ceiling."""
    assert sw.runtime_threshold_minutes("claude-sonnet-4-5[1m]") == 25
    assert sw.runtime_threshold_minutes("claude-sonnet-4-5-1m") == 25


def test_threshold_unknown_model_fails_safe_to_opus():
    assert sw.runtime_threshold_minutes("") == 25
    assert sw.runtime_threshold_minutes("some-unknown-model") == 25


# ---------------------------------------------------------------------------
# _pid_alive -- read-only liveness probe.
# ---------------------------------------------------------------------------


def test_pid_alive_true_for_own_process():
    import os

    assert sw._pid_alive(os.getpid()) is True


def test_pid_alive_false_for_zero_or_negative():
    assert sw._pid_alive(0) is False
    assert sw._pid_alive(-1) is False


# ---------------------------------------------------------------------------
# _parse_dispatch_row -- tab-separated row parsing, skip conditions.
# ---------------------------------------------------------------------------


def test_parse_dispatch_row_full_row():
    row = sw._parse_dispatch_row("agent-1\tclaude-opus-4\tsomething\t1700000000\n")
    assert row == ("agent-1", "claude-opus-4", 1700000000)


def test_parse_dispatch_row_skips_empty_agent_id():
    assert sw._parse_dispatch_row("\tmodel\tx\t1700000000") is None


def test_parse_dispatch_row_skips_non_numeric_dispatched_at():
    assert sw._parse_dispatch_row("agent-1\tmodel\tx\tnot-a-number") is None


def test_parse_dispatch_row_skips_zero_dispatched_at():
    assert sw._parse_dispatch_row("agent-1\tmodel\tx\t0") is None


def test_parse_dispatch_row_tolerates_short_legacy_row():
    assert sw._parse_dispatch_row("agent-1") is None  # no dispatched_at field at all


# ---------------------------------------------------------------------------
# _agent_completed -- completion-log grep-equivalent.
# ---------------------------------------------------------------------------


def test_agent_completed_false_when_log_absent(tmp_path):
    assert sw._agent_completed(tmp_path / "no-such-log.jsonl", "agent-1") is False


def test_agent_completed_true_on_match(tmp_path):
    log = tmp_path / "agent-audit.jsonl"
    log.write_text('{"agentId": "agent-1", "status": "done"}\n', encoding="utf-8")
    assert sw._agent_completed(log, "agent-1") is True


def test_agent_completed_false_on_no_match(tmp_path):
    log = tmp_path / "agent-audit.jsonl"
    log.write_text('{"agentId": "agent-2", "status": "done"}\n', encoding="utf-8")
    assert sw._agent_completed(log, "agent-1") is False


# ---------------------------------------------------------------------------
# _main_impl -- synchronous arming path (loop guard, session gate, lock,
# dispatch-file scan) exercised over a real filesystem fixture.
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()


_REAL_SESSION_ID = "12345678-1234-4123-8123-123456789abc"


def test_main_impl_stop_hook_active_short_circuits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sw, "_read_stdin", lambda timeout=2.0: '{"stop_hook_active": true}'
    )
    assert sw._main_impl() == 0


def test_main_impl_no_git_root_fails_open(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sw, "_read_stdin", lambda timeout=2.0: "{}")
    assert sw._main_impl() == 0


def test_main_impl_non_uuid_session_id_stands_down(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        sw, "_read_stdin", lambda timeout=2.0: '{"session_id": "not-a-uuid"}'
    )
    assert sw._main_impl() == 0


def test_main_impl_no_dispatch_file_removes_any_stale_lock(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        sw,
        "_read_stdin",
        lambda timeout=2.0: f'{{"session_id": "{_REAL_SESSION_ID}"}}',
    )
    assert sw._main_impl() == 0
    lock = repo / ".git" / "coordinator-sessions" / _REAL_SESSION_ID / "stop-watcher.pid"
    assert not lock.is_file()


def test_main_impl_launches_detached_watcher_and_writes_pid_lock(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.chdir(repo)

    sessions_dir = repo / ".git" / "coordinator-sessions"
    session_dir = sessions_dir / _REAL_SESSION_ID
    session_dir.mkdir(parents=True)
    dispatch_file = session_dir / "dispatched-agents.txt"
    now = int(time.time())
    dispatch_file.write_text(f"agent-1\tclaude-opus-4\tx\t{now}\n", encoding="utf-8")

    monkeypatch.setattr(
        sw,
        "_read_stdin",
        lambda timeout=2.0: f'{{"session_id": "{_REAL_SESSION_ID}"}}',
    )
    monkeypatch.setattr(sw, "_spawn_detached", lambda argv: 999999)

    assert sw._main_impl() == 0
    lock = session_dir / "stop-watcher.pid"
    assert lock.is_file()
    assert lock.read_text(encoding="utf-8") == "999999"


def test_main_impl_live_lock_does_not_stack_a_second_watcher(tmp_path, monkeypatch):
    import os

    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.chdir(repo)

    sessions_dir = repo / ".git" / "coordinator-sessions"
    session_dir = sessions_dir / _REAL_SESSION_ID
    session_dir.mkdir(parents=True)
    dispatch_file = session_dir / "dispatched-agents.txt"
    dispatch_file.write_text(
        f"agent-1\tclaude-opus-4\tx\t{int(time.time())}\n", encoding="utf-8"
    )
    lock = session_dir / "stop-watcher.pid"
    lock.write_text(str(os.getpid()), encoding="utf-8")  # a genuinely live PID

    spawn_calls = []
    monkeypatch.setattr(
        sw,
        "_read_stdin",
        lambda timeout=2.0: f'{{"session_id": "{_REAL_SESSION_ID}"}}',
    )
    monkeypatch.setattr(sw, "_spawn_detached", lambda argv: spawn_calls.append(argv) or 1)

    assert sw._main_impl() == 0
    assert spawn_calls == []  # never launched a second watcher


# ---------------------------------------------------------------------------
# _watch_main -- detached-child recheck: clears vs. wakes.
# ---------------------------------------------------------------------------


def test_watch_main_clears_when_agent_completed(tmp_path):
    sessions_dir = tmp_path / "coordinator-sessions"
    session_dir = sessions_dir / _REAL_SESSION_ID
    session_dir.mkdir(parents=True)
    dispatch_file = session_dir / "dispatched-agents.txt"
    dispatched_at = int(time.time()) - 60
    dispatch_file.write_text(f"agent-1\tclaude-opus-4\tx\t{dispatched_at}\n", encoding="utf-8")
    completion_log = sessions_dir / "logs" / "agent-audit.jsonl"
    completion_log.parent.mkdir(parents=True)
    completion_log.write_text('{"agentId": "agent-1"}\n', encoding="utf-8")
    lock = session_dir / "stop-watcher.pid"
    lock.write_text("123", encoding="utf-8")

    rc = sw._watch_main(
        [
            str(lock),
            str(dispatch_file),
            str(completion_log),
            "agent-1",
            "claude-opus-4",
            str(dispatched_at),
            "90",
            "0",
        ]
    )
    assert rc == 0
    assert not lock.is_file()


def test_watch_main_wakes_when_still_tracked(tmp_path, capsys):
    sessions_dir = tmp_path / "coordinator-sessions"
    session_dir = sessions_dir / _REAL_SESSION_ID
    session_dir.mkdir(parents=True)
    dispatch_file = session_dir / "dispatched-agents.txt"
    dispatched_at = int(time.time()) - 1800  # 30 min ago
    dispatch_file.write_text(f"agent-1\tclaude-opus-4\tx\t{dispatched_at}\n", encoding="utf-8")
    completion_log = sessions_dir / "logs" / "agent-audit.jsonl"  # no completion
    lock = session_dir / "stop-watcher.pid"
    lock.write_text("123", encoding="utf-8")

    rc = sw._watch_main(
        [
            str(lock),
            str(dispatch_file),
            str(completion_log),
            "agent-1",
            "claude-opus-4",
            str(dispatched_at),
            "90",
            "0",
        ]
    )
    assert rc == 2
    assert not lock.is_file()
    err = capsys.readouterr().err
    assert "RUNTIME TRIPWIRE" in err
    assert "agent-1" in err


def test_watch_main_malformed_args_fails_open():
    assert sw._watch_main(["too", "few", "args"]) == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
