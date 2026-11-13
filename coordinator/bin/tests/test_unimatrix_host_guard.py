"""Behavior pins for the Klabauter host-guard client: neutral fallback per host, bounded wall, deny pass-through."""
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
SCRIPT = BIN / "unimatrix-host-guard.py"
BUDGET = 0.5
CODEX_PAYLOAD = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                            "tool_input": {"command": "ls"}, "session_id": "s1", "cwd": "/x"})
AGY_PAYLOAD = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "run_command",
                          "tool_input": {"CommandLine": "ls"}, "session_id": "s1", "cwd": "/x"})


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_host_guard", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hg = load()
NEUTRAL = {"agy": {"decision": "ask"}, "codex": {}}


@pytest.fixture(autouse=True)
def settings_home(tmp_path, monkeypatch):
    home = tmp_path / "settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    return home


def degrade_records(home):
    log = home / "host-hooks" / hg.DEGRADE_LOG_NAME
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def make_engine(tmp_path, shim_src):
    root = tmp_path / "engine"
    pkg = root / "coordinator_core" / "host_hooks"
    pkg.mkdir(parents=True)
    (root / "coordinator_core" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "shim.py").write_text(shim_src)
    return str(root)


ECHO_SHIM = """
import sys
def main():
    sys.stdin.read()
    {body}
main()
"""


def shim(body):
    return ECHO_SHIM.format(body=body)


@pytest.mark.parametrize("host", ["agy", "codex"])
def test_no_engine_root_is_neutral_degraded(host):
    assert hg.guard(host, CODEX_PAYLOAD, None) == (NEUTRAL[host], "degraded")
    assert hg.guard(host, CODEX_PAYLOAD, "/no/such/dir") == (NEUTRAL[host], "degraded")


@pytest.mark.parametrize("host", ["agy", "codex"])
def test_shim_raising_is_neutral(tmp_path, host):
    root = make_engine(tmp_path, "raise RuntimeError('boom')\n")
    assert hg.guard(host, CODEX_PAYLOAD, root) == (NEUTRAL[host], "degraded")


@pytest.mark.parametrize("host", ["agy", "codex"])
@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", "{"])
def test_malformed_stdin_is_neutral(tmp_path, host, stdin):
    root = make_engine(tmp_path, shim("print('{\"decision\": \"deny\", \"reason\": \"r\"}')"))
    assert hg.guard(host, stdin, root) == (NEUTRAL[host], "degraded")


@pytest.mark.deliberate_wall_clock(reason="bounds how long a blocking shim holds the guard before the budget deadline answers neutral, a wait CPU time cannot see")
@pytest.mark.parametrize("src", ["import time\ntime.sleep(60)\n", None])
@pytest.mark.parametrize("host", ["agy", "codex"])
def test_blocking_shim_returns_neutral_within_budget(tmp_path, host, src):
    body = src or shim("import time; time.sleep(60)")
    root = make_engine(tmp_path, body)
    start = time.monotonic()
    result = hg.guard(host, CODEX_PAYLOAD, root, budget_secs=BUDGET)
    assert time.monotonic() - start < BUDGET + 0.5
    assert result == (NEUTRAL[host], "degraded")


def test_timed_out_child_is_killed_and_reaped(tmp_path, monkeypatch):
    root = make_engine(tmp_path, shim("import time; time.sleep(60)"))
    spawned = []
    real = subprocess.Popen

    def spy(*a, **k):
        p = real(*a, **k)
        spawned.append(p)
        return p

    monkeypatch.setattr(hg.subprocess, "Popen", spy)
    assert hg.guard("codex", CODEX_PAYLOAD, root, budget_secs=0.3)[1] == "degraded"
    assert len(spawned) == 1 and spawned[0].poll() is not None


@pytest.mark.parametrize("host", ["agy", "codex"])
@pytest.mark.parametrize("body", [
    "print('')",
    "sys.stdout.write('{\"decision\": \"den')",
    "print('not json')",
    "print('{}'); print('{}')",
    "print('[1]')",
    "sys.exit(3)",
])
def test_bad_stdout_is_neutral_degraded(tmp_path, host, body):
    root = make_engine(tmp_path, shim(body))
    assert hg.guard(host, CODEX_PAYLOAD, root) == (NEUTRAL[host], "degraded")


def test_agy_answer_without_decision_is_degraded(tmp_path):
    root = make_engine(tmp_path, shim("print('{}')"))
    assert hg.guard("agy", AGY_PAYLOAD, root) == ({"decision": "ask"}, "degraded")


def test_codex_bare_empty_object_is_neutral_unknown_never_engine(tmp_path):
    root = make_engine(tmp_path, shim("print('{}')"))
    assert hg.guard("codex", CODEX_PAYLOAD, root) == ({}, "neutral-unknown")


def test_codex_internal_error_style_neutral_is_not_engine(tmp_path):
    root = make_engine(tmp_path, shim("sys.stderr.write('caught internal error\\n'); print('{}')"))
    answer, state = hg.guard("codex", CODEX_PAYLOAD, root)
    assert answer == {} and state != "engine"


GOOD_INNER = {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "no"}


@pytest.mark.parametrize("bad", [
    {"decision": "deny"},
    {"continue": False},
    {"decision": "deny", "reason": "r"},
    {"hookSpecificOutput": {**GOOD_INNER, "hookEventName": "PostToolUse"}},
    {"hookSpecificOutput": {**GOOD_INNER, "permissionDecision": "allow"}},
    {"hookSpecificOutput": {**GOOD_INNER, "permissionDecision": "ask"}},
    {"hookSpecificOutput": {**GOOD_INNER, "permissionDecisionReason": 7}},
    {"hookSpecificOutput": {**GOOD_INNER, "extra": 1}},
    {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}},
    {"hookSpecificOutput": "deny"},
    {"hookSpecificOutput": GOOD_INNER, "continue": False},
    {"hookSpecificOutput": GOOD_INNER, "decision": "deny"},
    {"unknown": True},
])
def test_malformed_codex_output_is_neutral_degraded(tmp_path, bad):
    root = make_engine(tmp_path, shim(f"print({json.dumps(json.dumps(bad))})"))
    assert hg.guard("codex", CODEX_PAYLOAD, root) == ({}, "degraded")


def test_stray_stderr_never_reaches_stdout(tmp_path):
    root = make_engine(tmp_path, shim("sys.stderr.write('noise\\n'); print('{\"decision\": \"ask\"}')"))
    proc = subprocess.run([sys.executable, str(SCRIPT), "--host", "agy", "--engine-root", root],
                          input=AGY_PAYLOAD, capture_output=True, text=True)
    assert proc.stdout.strip() == '{"decision": "ask"}'
    assert "noise" not in proc.stdout


def test_stub_deny_passes_through_untouched(tmp_path):
    deny = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "no"}}
    root = make_engine(tmp_path, shim(f"print({json.dumps(json.dumps(deny))})"))
    assert hg.guard("codex", CODEX_PAYLOAD, root) == (deny, "engine")
    agy_deny = {"decision": "deny", "reason": "no"}
    (tmp_path / "b").mkdir()
    root2 = make_engine(tmp_path / "b", shim(f"print({json.dumps(json.dumps(agy_deny))})"))
    assert hg.guard("agy", AGY_PAYLOAD, root2) == (agy_deny, "engine")


@pytest.mark.parametrize("host,payload", [("agy", AGY_PAYLOAD), ("codex", CODEX_PAYLOAD), ("codex", "garbage"), ("agy", "")])
def test_cli_one_json_line_exit_zero(tmp_path, host, payload):
    root = make_engine(tmp_path, "raise SystemExit(7)\n")
    proc = subprocess.run([sys.executable, str(SCRIPT), "--host", host, "--engine-root", root],
                          input=payload, capture_output=True, text=True)
    assert proc.returncode == 0
    lines = proc.stdout.splitlines()
    assert len(lines) == 1 and json.loads(lines[0]) == NEUTRAL[host]
    assert proc.stderr.strip() == "degraded"


def test_cli_without_args_is_neutral_exit_zero():
    proc = subprocess.run([sys.executable, str(SCRIPT)], input="", capture_output=True, text=True)
    assert proc.returncode == 0
    assert len(proc.stdout.splitlines()) == 1 and json.loads(proc.stdout) == {}


def _real_engine_root():
    try:
        sys.path.insert(0, str(BIN.parent / "hooks" / "scripts"))
        import _engine_root
        root, _kind, _prov = _engine_root.resolve_claude_klabauter_root_with_provenance()
        root = Path(root)
        return str(root) if (root / "coordinator_core" / "host_hooks" / "codex_adapter.py").is_file() else None
    except Exception:
        return None


def test_real_codex_adapter_renders_nested_deny():
    root = _real_engine_root()
    if root is None:
        pytest.skip("engine root unresolvable: published claude-klabauter not found")
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "s",
                          "cwd": root, "tool_input": {"command": "p4 submit -d x"}})
    answer, state = hg.guard("codex", payload, root, budget_secs=5.0)
    assert state == "engine" and "hookSpecificOutput" in answer, (answer, state)
    out = answer["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    assert "decision" not in answer


@pytest.mark.parametrize("decision", ["allow", "ask", "force_ask"])
def test_agy_engine_non_deny_never_passes_allow_through(tmp_path, decision):
    # The warm engine answers a neutral agy command with "allow"; the guard must answer ask instead.
    root = make_engine(tmp_path, shim(f"print(json.dumps({{'decision': '{decision}'}}))"))
    answer, state = hg.guard("agy", AGY_PAYLOAD, root)
    assert answer == {"decision": "ask"} and state != "engine"


def test_agy_ask_with_advisory_reason_reaches_output_intact_never_allow(tmp_path):
    from coordinator_core.host_hooks.agy_adapter import AgyAdapter
    rendered = AgyAdapter().render(
        AgyAdapter().adapt_event({"toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}),
        {"decision": "allow", "additionalContext": "advisory nag"})
    root = make_engine(tmp_path, shim(f"print({json.dumps(json.dumps(rendered))})"))
    answer, state = hg.guard("agy", AGY_PAYLOAD, root)
    assert answer == {"decision": "ask", "reason": "advisory nag"} and state == hg.NEUTRAL_UNKNOWN


def test_timeout_degrade_records_cause_and_child_cpu(tmp_path, settings_home):
    root = make_engine(tmp_path, shim("import time; time.sleep(60)"))
    assert hg.guard("codex", CODEX_PAYLOAD, root, budget_secs=0.3) == ({}, "degraded")
    (rec,) = degrade_records(settings_home)
    assert rec["cause"] == "timeout" and rec["host"] == "codex" and rec["budget_secs"] == 0.3
    assert rec["child_cpu_secs"] is None or rec["child_cpu_secs"] >= 0


def test_other_degrade_causes_are_distinguishable(tmp_path, settings_home):
    hg.guard("codex", CODEX_PAYLOAD, None)
    hg.guard("codex", "not json", make_engine(tmp_path, shim("print('{}')")))
    hg.guard("codex", CODEX_PAYLOAD, make_engine(tmp_path / "a", "raise RuntimeError('boom')\n"))
    hg.guard("codex", CODEX_PAYLOAD, make_engine(tmp_path / "b", shim("print('[1]')")))
    assert [r["cause"] for r in degrade_records(settings_home)] == [
        "no_engine_root", "bad_stdin", "nonzero_exit", "invalid_answer"]


def test_engine_answer_and_neutral_unknown_record_nothing(tmp_path, settings_home):
    hg.guard("codex", CODEX_PAYLOAD, make_engine(tmp_path, shim("print('{}')")))
    assert degrade_records(settings_home) == []


def test_unwritable_record_never_fails_the_guard(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(blocker))
    assert hg.guard("codex", CODEX_PAYLOAD, None) == ({}, "degraded")


def test_default_budget_is_half_a_second():
    assert hg.DEFAULT_BUDGET_SECS == 0.5


def test_cli_timeout_at_default_budget_records_and_stays_exit_zero(tmp_path, settings_home):
    root = make_engine(tmp_path, shim("import time; time.sleep(60)"))
    r = subprocess.run([sys.executable, str(SCRIPT), "--host", "codex", "--engine-root", root],
                       input=CODEX_PAYLOAD, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and json.loads(r.stdout) == {} and r.stderr.strip() == "degraded"
    (rec,) = degrade_records(settings_home)
    assert rec["cause"] == "timeout" and rec["budget_secs"] == 0.5
