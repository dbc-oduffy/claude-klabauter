"""Tests for the submit-shaped swap harness: verdicts, the report, the hook-timeout read, the door
dispatch and `run`/`main` wired against stand-ins for the real pair."""

from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks import land_swap_hook_window as lw
from coordinator_core.benchmarks import swap_continuous_caller as scc
from coordinator_core.benchmarks import swap_submit_caller as ssc

pytestmark = pytest.mark.complete

TIMEOUT_MS = 12000.0
_DENY = json.dumps(
    {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                            "permissionDecisionReason": "Re-submit once; list recent runs under state/warp first."}}
)
_FAIL_OPEN = json.dumps(
    {"systemMessage": "coordinator: engine failed (no response within 8000ms of delivery); guard not evaluated, command allowed."}
)
_SPOOLED = json.dumps({"systemMessage": "coordinator: SPOOLED as abc123: it fires automatically once the engine is warm. Do not resubmit."})


def _sub(send: float, wall: float, outcome: str = ssc.LANDED, process_ms: float = 1.0, text: str = "") -> ssc.Submit:
    return ssc.Submit(send, wall, process_ms, outcome, text)


def test_a_deny_with_a_remedy_is_refused_and_a_spool_is_refused() -> None:
    assert ssc.classify(_DENY.encode(), 0, 500.0, TIMEOUT_MS)[0] == ssc.REFUSED
    assert ssc.classify(_SPOOLED.encode(), 0, 500.0, TIMEOUT_MS)[0] == ssc.REFUSED


def test_no_answer_and_the_fail_open_advisory_are_silent_drops_with_text() -> None:
    outcome, text = ssc.classify(b"", 0, 500.0, TIMEOUT_MS, b"boom")
    assert outcome == ssc.SILENT_DROP and "no hook answer" in text
    outcome, text = ssc.classify(_FAIL_OPEN.encode(), 0, 500.0, TIMEOUT_MS)
    assert outcome == ssc.SILENT_DROP and "guard not evaluated" in text


def test_a_door_past_the_hook_timeout_is_a_drop_whatever_it_printed() -> None:
    outcome, text = ssc.classify(_DENY.encode(), 0, TIMEOUT_MS, TIMEOUT_MS)
    assert outcome == ssc.SILENT_DROP and "past the 12000ms hook timeout" in text
    assert ssc.classify(_DENY.encode(), 0, TIMEOUT_MS - 1.0, TIMEOUT_MS)[0] == ssc.REFUSED


def test_exit_two_with_stderr_is_a_refusal() -> None:
    assert ssc.classify(b"", 2, 10.0, TIMEOUT_MS, b"submit refused: remedy")[0] == ssc.REFUSED


def test_report_counts_verdicts_and_reduces_wall_and_process_time() -> None:
    report = ssc.SubmitReport(
        requests=(_sub(1.0, 40.0), _sub(1.1, 90.0, ssc.REFUSED, 7.0), _sub(1.2, 13000.0, ssc.SILENT_DROP, 3.0)),
        hook_timeout_s=12.0,
    )
    assert (report.submits, report.landed, report.refused, report.silent_drop) == (3, 1, 1, 1)
    assert report.max_door_wall_ms == 13000.0 and report.max_process_ms == 7.0
    line = report.falsifier_line()
    for key in ("submits=3", "landed=1", "refused=1", "silent_drop=1", "max_door_wall_ms=13000", "max_process_ms=7"):
        assert key in line


def test_budget_holds_only_with_no_drop_wall_under_timeout_and_process_under_bar() -> None:
    clean = (_sub(1.0, 40.0), _sub(1.1, 90.0, ssc.REFUSED))
    assert ssc.SubmitReport(requests=clean, hook_timeout_s=12.0).within_budget()
    assert not ssc.SubmitReport(requests=(*clean, _sub(1.2, 10.0, ssc.SILENT_DROP)), hook_timeout_s=12.0).within_budget()
    assert not ssc.SubmitReport(requests=(*clean, _sub(1.2, 12500.0)), hook_timeout_s=12.0).within_budget()
    assert not ssc.SubmitReport(requests=(*clean, _sub(1.2, 10.0, process_ms=250.0)), hook_timeout_s=12.0).within_budget()
    assert ssc.SubmitReport(requests=(), hook_timeout_s=12.0).within_budget()


def test_phase_lines_attribute_each_submit_to_the_phase_it_was_sent_in() -> None:
    phases = {"marker_take": 100.0, "successor_spawn": 101.0}
    report = ssc.SubmitReport(
        requests=(_sub(99.0, 5.0), _sub(100.5, 9.0), _sub(101.5, 800.0, ssc.SILENT_DROP)), hook_timeout_s=12.0, phases=phases
    )
    assert report.phase_of(99.0) == ssc.PRE_SWAP and report.phase_of(101.5) == "successor_spawn"
    lines = report.phase_lines()
    assert len(lines) == 3 and "silent_drop=1" in lines[2] and "max_door_wall_ms=800.0" in lines[2]
    assert "successor_spawn" in report.slowest_line() and "silent_drop" in report.slowest_line()


def test_hook_timeout_is_read_through_the_hook_module_and_the_env_override(monkeypatch) -> None:
    from coordinator_core.hooks import submit_engine_ask as hook

    monkeypatch.delenv(ssc.HOOK_TIMEOUT_ENV, raising=False)
    assert ssc.hook_timeout_s() == hook._CALLER_BUDGET_S
    monkeypatch.setenv(ssc.HOOK_TIMEOUT_ENV, "7.5")
    assert ssc.hook_timeout_s() == 7.5
    monkeypatch.setenv(ssc.HOOK_TIMEOUT_ENV, "junk")
    assert ssc.hook_timeout_s() == hook._CALLER_BUDGET_S


def test_an_unreadable_hook_timeout_refuses_naming_the_key(monkeypatch) -> None:
    from coordinator_core.hooks import submit_engine_ask as hook

    monkeypatch.delenv(ssc.HOOK_TIMEOUT_ENV, raising=False)
    monkeypatch.setattr(hook, "_CALLER_BUDGET_S", None)
    with pytest.raises(ssc.HookTimeoutUnreadable, match="_CALLER_BUDGET_S"):
        ssc.hook_timeout_s()


def test_the_payload_is_a_workflow_submission_of_a_pooled_sizing_with_a_fresh_tool_use_id(tmp_path: Path) -> None:
    from coordinator_core.hooks.submit_engine_ask import is_submission

    first, second = (json.loads(ssc.submit_payload(tmp_path, n)) for n in (1, 2))
    assert is_submission(first) and first["tool_input"]["args"]["sizing"] != second["tool_input"]["args"]["sizing"]
    assert first["tool_use_id"] != second["tool_use_id"] and first["cwd"] == str(tmp_path)
    assert json.loads(ssc.submit_payload(tmp_path, 1 + ssc.SIZING_POOL))["tool_input"] == first["tool_input"]


def _door(monkeypatch, *, stdout: str = "", returncode: int = 0, expired: bool = False) -> tuple:
    seen: list = []

    def fake(argv, **kw):
        seen.append((argv, kw))
        if expired:
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        return subprocess.CompletedProcess(argv, returncode, stdout.encode(), b""), 4.5

    monkeypatch.setattr(scc, "_run_door", fake)
    return ssc.door_submit_dispatch(Path("root"), Path("hook-run.exe"), Path("repo"), 12.0), seen


def test_door_dispatch_runs_the_submit_op_in_hook_mode_and_classifies_the_answer(monkeypatch) -> None:
    dispatch, seen = _door(monkeypatch, stdout=_DENY)
    record = dispatch()
    argv, kw = seen[0]
    assert argv[1] == ssc.OP == "hooks.submit_engine_ask" and kw["env"]["COORDINATOR_DOOR_STDIN_MODE"] == "hook"
    assert kw["timeout"] == 12.0 + ssc.KILL_GRACE_SECS
    assert (record.outcome, record.process_ms) == (ssc.REFUSED, 4.5) and "Re-submit once" in record.text


def test_door_dispatch_records_a_door_the_harness_had_to_kill_as_a_drop(monkeypatch) -> None:
    dispatch, _ = _door(monkeypatch, expired=True)
    record = dispatch()
    assert record.outcome == ssc.SILENT_DROP and "killed by the harness" in record.text


def test_the_submit_loop_issues_back_to_back_until_stopped() -> None:
    caller = ssc.ThreadSubmitCaller(lambda: _sub(0.0, 1.0))
    caller.start()
    import time

    time.sleep(0.05)
    records = caller.stop()
    assert len(records) > 3 and all(r.outcome == ssc.LANDED for r in records)


def test_install_door_refuses_without_an_image_and_installs_under_the_entrypoint_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ssc.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="no door image"):
        ssc.install_door(tmp_path, None)
    image = tmp_path / "built-door"
    image.write_bytes(b"x")
    assert ssc.install_door(tmp_path, image).name == ssc.ENTRYPOINT


def test_run_drives_the_swap_with_the_caller_across_it_and_reports_the_timeout(monkeypatch, tmp_path: Path) -> None:
    @contextlib.contextmanager
    def fixture(_base, _source):
        yield tmp_path / "engine", tmp_path / "repo"

    def handover(root, *, copy_secs):
        with ssc.scc.breadcrumb.hold_publish_marker(root, holder="test"):
            pass
        return True

    monkeypatch.setattr(ssc, "fixture_engine", fixture)
    monkeypatch.setattr(ssc, "install_door", lambda root, image: Path("hook-run.exe"))
    monkeypatch.setattr(ssc, "door_submit_dispatch", lambda *a: (lambda: _sub(0.0, 1.0)))
    monkeypatch.setattr(lw, "production_handover", handover)
    monkeypatch.setattr(scc, "PRE_ROLL_SECS", 0.02)
    monkeypatch.setattr(scc, "TAIL_AFTER_RELEASE_SECS", 0.02)
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path / "rt"))

    report = ssc.run(work_dir=tmp_path, caller_in_process=True)

    assert report.submits > 1 and report.successor_answered_before_release is True
    assert report.hook_timeout_s == ssc.hook_timeout_s() and "marker_take" in report.phases


def test_main_prints_the_falsifier_line_the_timeline_and_exits_by_budget(monkeypatch, capsys) -> None:
    phases = {name: 10.0 + i for i, name in enumerate(scc.PHASES)}
    dirty = ssc.SubmitReport(requests=(_sub(10.0, 5.0, ssc.SILENT_DROP, text="gone"),), hook_timeout_s=12.0, phases=phases)
    monkeypatch.setattr(ssc, "run", lambda **kw: dirty)

    assert ssc.main([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("swap_submit_caller: submits=1 landed=0 refused=0 silent_drop=1 max_door_wall_ms=5")
    assert sum(line.startswith("  phase ") for line in out) == len(scc.PHASES)
    assert any("silent_drop x1: gone" in line for line in out)
    assert ssc.main(["--assert-budget"]) == 1


def test_main_refuses_naming_the_key_when_the_hook_timeout_is_unreadable(monkeypatch, capsys) -> None:
    def refuse(**kw):
        raise ssc.HookTimeoutUnreadable("missing key submit_engine_ask._CALLER_BUDGET_S")

    monkeypatch.setattr(ssc, "run", refuse)
    assert ssc.main([]) == 2
    assert "missing key submit_engine_ask._CALLER_BUDGET_S" in capsys.readouterr().out
