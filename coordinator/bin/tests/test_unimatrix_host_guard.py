"""Behavior pins for the Klabauter host-guard client: open answer per host, bounded wall, no child, deny pass-through."""
import importlib.util
import json
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
REPO = BIN.parents[1]
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
NEUTRAL = {"agy": {"decision": "ask"}, "codex": {}, "grok": {}}
GROK_PAYLOAD = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "edit_file",
                           "tool_input": {"path": "a.py"}, "cwd": "/x"})
GROK_DENY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                    "permissionDecisionReason": "no"}}


@pytest.fixture(autouse=True)
def settings_home(tmp_path, monkeypatch):
    home = tmp_path / "settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path / "warm-runtime"))
    return home


def degrade_records(home):
    log = home / "host-hooks" / hg.DEGRADE_LOG_NAME
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def assert_open(host, answer):
    """The host's open answer; agy additionally carries a non-empty advisory reason."""
    assert {k: v for k, v in answer.items() if k != "reason"} == NEUTRAL[host]
    if host == "agy":
        assert isinstance(answer.get("reason"), str) and answer["reason"]
    else:
        assert "reason" not in answer


def stub_shim(monkeypatch, answer=None, run=None):
    """Seam the shim import: guard() calls `run(host, stdin)` on its walled thread, no engine tree needed."""
    fn = run or (lambda host, text: answer)
    monkeypatch.setattr(hg, "_load_shim", lambda root: types.SimpleNamespace(run=fn))


def make_engine(tmp_path, shim_src):
    """A stub engine tree on disk, for the CLI tests that run the guard script in its own interpreter."""
    root = tmp_path / "engine"
    pkg = root / "coordinator_core" / "host_hooks"
    pkg.mkdir(parents=True)
    (root / "coordinator_core" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "shim.py").write_text(shim_src)
    return str(root)


def shim_src(body):
    return "import sys\n\ndef run(host, stdin_text):\n    " + body + "\n"


@pytest.mark.parametrize("host", ["agy", "codex"])
def test_no_engine_root_is_open_degraded(host):
    for root in (None, "/no/such/dir"):
        answer, state = hg.guard(host, CODEX_PAYLOAD, root)
        assert state == "degraded"
        assert_open(host, answer)


@pytest.mark.parametrize("host", ["agy", "codex"])
def test_shim_load_failure_is_open_import_error(tmp_path, monkeypatch, settings_home, host):
    def boom(root):
        raise ImportError("no shim")

    monkeypatch.setattr(hg, "_load_shim", boom)
    answer, state = hg.guard(host, CODEX_PAYLOAD, str(tmp_path))
    assert state == "degraded"
    assert_open(host, answer)
    assert [r["cause"] for r in degrade_records(settings_home)] == ["import_error"]


@pytest.mark.parametrize("host", ["agy", "codex"])
def test_shim_run_raising_is_open_degraded(tmp_path, monkeypatch, host):
    def boom(h, t):
        raise RuntimeError("boom")

    stub_shim(monkeypatch, run=boom)
    answer, state = hg.guard(host, CODEX_PAYLOAD, str(tmp_path))
    assert state == "degraded"
    assert_open(host, answer)


@pytest.mark.parametrize("host", ["agy", "codex"])
@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", "{"])
def test_malformed_stdin_is_open(tmp_path, monkeypatch, host, stdin):
    stub_shim(monkeypatch, {"decision": "deny", "reason": "r"})
    answer, state = hg.guard(host, stdin, str(tmp_path))
    assert state == "degraded"
    assert_open(host, answer)


@pytest.mark.deliberate_wall_clock(reason="bounds how long a blocking shim holds the guard before the budget deadline answers open, a wait CPU time cannot see")
@pytest.mark.parametrize("host", ["agy", "codex"])
def test_blocking_shim_returns_open_within_budget_recording_timeout(tmp_path, monkeypatch, settings_home, host):
    release = threading.Event()
    stub_shim(monkeypatch, run=lambda h, t: release.wait(60))
    wall0, cpu0 = time.monotonic(), time.process_time()
    try:
        answer, state = hg.guard(host, CODEX_PAYLOAD, str(tmp_path), budget_secs=BUDGET)
    finally:
        release.set()
    assert time.monotonic() - wall0 < BUDGET + 0.5
    assert time.process_time() - cpu0 < 0.1
    assert state == "degraded"
    assert_open(host, answer)
    (rec,) = degrade_records(settings_home)
    assert rec["cause"] == "timeout" and rec["host"] == host and rec["budget_secs"] == BUDGET
    assert isinstance(rec["cpu_secs"], float) and rec["cpu_secs"] >= 0
    assert "child_cpu_secs" not in rec


def test_agy_degrade_advisory_names_the_cause(tmp_path):
    answer, _ = hg.guard("agy", CODEX_PAYLOAD, None)
    assert answer == {"decision": "ask", "reason": "Klabauter guard unavailable (no_engine_root); proceeding open"}


def test_guard_hot_path_spawns_no_python_child_and_a_cold_engine_answers_open(monkeypatch, settings_home):
    """Real shim from the real repo root, warm dial forced to a miss, Popen armed to raise."""
    monkeypatch.syspath_prepend(str(REPO))
    from coordinator_core.warm import client

    spawns = []

    def no_spawn(*a, **k):
        spawns.append(a)
        raise AssertionError("the guard hot path spawned a child process")

    monkeypatch.setattr(client, "try_warm_dispatch", lambda *a, **k: None)
    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    for host, payload in (("codex", CODEX_PAYLOAD), ("agy", AGY_PAYLOAD)):
        answer, state = hg.guard(host, payload, str(REPO))
        assert state in ("neutral-unknown", "degraded"), (host, answer, state)
        assert_open(host, answer)
        assert state != "engine"
    assert spawns == []


def test_guard_module_carries_no_subprocess_use():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "subprocess" not in text and "sys.executable" not in text


@pytest.mark.parametrize("host", ["agy", "codex"])
@pytest.mark.parametrize("answer", [
    None,
    "",
    "not a dict",
    [1],
    {"unknown": True},
])
def test_bad_shim_answer_is_open_degraded(tmp_path, monkeypatch, settings_home, host, answer):
    stub_shim(monkeypatch, answer)
    got, state = hg.guard(host, CODEX_PAYLOAD, str(tmp_path))
    assert state == "degraded"
    assert_open(host, got)
    assert degrade_records(settings_home)[-1]["cause"] == "invalid_answer"


def test_agy_answer_without_decision_is_degraded(tmp_path, monkeypatch):
    stub_shim(monkeypatch, {})
    answer, state = hg.guard("agy", AGY_PAYLOAD, str(tmp_path))
    assert state == "degraded"
    assert_open("agy", answer)


def test_codex_bare_empty_object_is_neutral_unknown_never_engine(tmp_path, monkeypatch):
    stub_shim(monkeypatch, {})
    assert hg.guard("codex", CODEX_PAYLOAD, str(tmp_path)) == ({}, "neutral-unknown")


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
def test_malformed_codex_output_is_open_degraded(tmp_path, monkeypatch, bad):
    stub_shim(monkeypatch, bad)
    assert hg.guard("codex", CODEX_PAYLOAD, str(tmp_path)) == ({}, "degraded")


CODEX_ADVISORY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "XS budget spent"}}
CODEX_ADVISORY_ALLOW = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                "additionalContext": "XS budget spent"}}


@pytest.mark.parametrize("advisory", [CODEX_ADVISORY, CODEX_ADVISORY_ALLOW])
def test_codex_advisory_reaches_output_intact_as_neutral_unknown(tmp_path, monkeypatch, advisory):
    assert hg._valid_answer("codex", advisory) is True
    stub_shim(monkeypatch, advisory)
    assert hg.guard("codex", CODEX_PAYLOAD, str(tmp_path)) == (advisory, "neutral-unknown")


@pytest.mark.parametrize("bad", [
    {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ""}},
    {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": 3}},
    {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "x"}},
    {"hookSpecificOutput": {**CODEX_ADVISORY_ALLOW["hookSpecificOutput"], "permissionDecision": "deny"}},
    {"hookSpecificOutput": {**CODEX_ADVISORY_ALLOW["hookSpecificOutput"], "permissionDecisionReason": "r"}},
    {"hookSpecificOutput": {**CODEX_ADVISORY["hookSpecificOutput"], "extra": 1}},
    {**CODEX_ADVISORY, "continue": False},
])
def test_codex_malformed_advisory_degrades(bad):
    assert hg._valid_answer("codex", bad) is False


def test_stray_stderr_never_reaches_stdout(tmp_path):
    root = make_engine(tmp_path, shim_src("sys.stderr.write('noise\\n'); return {'decision': 'ask'}"))
    proc = subprocess.run([sys.executable, str(SCRIPT), "--host", "agy", "--engine-root", root],
                          input=AGY_PAYLOAD, capture_output=True, text=True)
    assert proc.stdout.strip() == '{"decision": "ask"}'
    assert "noise" not in proc.stdout


def test_stub_deny_passes_through_untouched(tmp_path, monkeypatch):
    deny = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "no"}}
    stub_shim(monkeypatch, deny)
    assert hg.guard("codex", CODEX_PAYLOAD, str(tmp_path)) == (deny, "engine")
    agy_deny = {"decision": "deny", "reason": "no"}
    stub_shim(monkeypatch, agy_deny)
    assert hg.guard("agy", AGY_PAYLOAD, str(tmp_path)) == (agy_deny, "engine")


@pytest.mark.parametrize("host,payload", [("agy", AGY_PAYLOAD), ("codex", CODEX_PAYLOAD), ("codex", "garbage"), ("agy", "")])
def test_cli_one_json_line_exit_zero(tmp_path, host, payload):
    root = make_engine(tmp_path, "raise SystemExit(7)\n")
    proc = subprocess.run([sys.executable, str(SCRIPT), "--host", host, "--engine-root", root],
                          input=payload, capture_output=True, text=True)
    assert proc.returncode == 0
    lines = proc.stdout.splitlines()
    assert len(lines) == 1
    assert_open(host, json.loads(lines[0]))
    assert proc.stderr.strip() == "degraded"


def test_cli_without_args_is_open_exit_zero():
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
def test_agy_engine_non_deny_never_passes_allow_through(tmp_path, monkeypatch, decision):
    # The warm engine answers a neutral agy command with "allow"; the guard must answer ask instead.
    stub_shim(monkeypatch, {"decision": decision})
    answer, state = hg.guard("agy", AGY_PAYLOAD, str(tmp_path))
    assert answer["decision"] == "ask" and state != "engine"
    if decision != "force_ask":
        assert answer == {"decision": "ask"}


def test_agy_ask_with_advisory_reason_reaches_output_intact_never_allow(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(REPO))
    from coordinator_core.host_hooks.agy_adapter import AgyAdapter
    rendered = AgyAdapter().render(
        AgyAdapter().adapt_event({"toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}),
        {"decision": "allow", "additionalContext": "advisory nag"})
    stub_shim(monkeypatch, rendered)
    answer, state = hg.guard("agy", AGY_PAYLOAD, str(tmp_path))
    assert answer == {"decision": "ask", "reason": "advisory nag"} and state == hg.NEUTRAL_UNKNOWN


def test_other_degrade_causes_are_distinguishable(tmp_path, monkeypatch, settings_home):
    hg.guard("codex", CODEX_PAYLOAD, None)
    stub_shim(monkeypatch, {})
    hg.guard("codex", "not json", str(tmp_path))
    stub_shim(monkeypatch, [1])
    hg.guard("codex", CODEX_PAYLOAD, str(tmp_path))
    monkeypatch.setattr(hg, "_load_shim", lambda root: (_ for _ in ()).throw(ImportError("x")))
    hg.guard("codex", CODEX_PAYLOAD, str(tmp_path))
    assert [r["cause"] for r in degrade_records(settings_home)] == [
        "no_engine_root", "bad_stdin", "invalid_answer", "import_error"]


def test_engine_answer_and_neutral_unknown_record_nothing(tmp_path, monkeypatch, settings_home):
    stub_shim(monkeypatch, {})
    hg.guard("codex", CODEX_PAYLOAD, str(tmp_path))
    assert degrade_records(settings_home) == []


def test_unwritable_record_never_fails_the_guard(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(blocker))
    assert hg.guard("codex", CODEX_PAYLOAD, None) == ({}, "degraded")


def test_default_budget_is_half_a_second():
    assert hg.DEFAULT_BUDGET_SECS == 0.5


def test_cli_timeout_at_default_budget_records_and_stays_exit_zero(tmp_path, settings_home):
    root = make_engine(tmp_path, "import time\n\ndef run(host, stdin_text):\n    time.sleep(60)\n")
    r = subprocess.run([sys.executable, str(SCRIPT), "--host", "codex", "--engine-root", root],
                       input=CODEX_PAYLOAD, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and json.loads(r.stdout) == {} and r.stderr.strip() == "degraded"
    (rec,) = degrade_records(settings_home)
    assert rec["cause"] == "timeout" and rec["budget_secs"] == 0.5


def test_grok_neutral_is_the_no_change_answer_and_agrees_with_contract(monkeypatch):
    assert hg._neutral("grok") == {}
    monkeypatch.syspath_prepend(str(REPO))
    from coordinator_core.host_hooks import contract
    assert "grok" in contract.HOSTS
    expected = {k: v for k, v in contract.neutral_fallback("grok").items() if k != "reason"}
    assert hg.NEUTRAL["grok"] == expected


@pytest.mark.parametrize("answer", [
    {}, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}},
    {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                            "permissionDecisionReason": "r"}}, GROK_DENY])
def test_grok_valid_answer_shapes(answer):
    assert hg._valid_answer("grok", answer) is True


@pytest.mark.parametrize("answer", [
    {"decision": "deny"}, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "block"}},
    {"hookSpecificOutput": {"hookEventName": "Stop", "permissionDecision": "deny"}},
    {"hookSpecificOutput": {"permissionDecision": "deny"}}, {"x": 1}, []])
def test_grok_invalid_answer_shapes(answer):
    assert hg._valid_answer("grok", answer) is False


@pytest.mark.parametrize("answer", [
    {},
    {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}])
def test_grok_neutral_or_allow_is_neutral_unknown_never_engine(tmp_path, monkeypatch, answer):
    stub_shim(monkeypatch, answer)
    _, state = hg.guard("grok", GROK_PAYLOAD, str(tmp_path))
    assert state == hg.NEUTRAL_UNKNOWN and state != "engine"


GROK_ALLOW_ADVISORY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                               "additionalContext": "advisory text"}}


def test_grok_allow_with_additional_context_is_valid_and_reaches_output_intact(tmp_path, monkeypatch):
    assert hg._valid_answer("grok", GROK_ALLOW_ADVISORY) is True
    stub_shim(monkeypatch, GROK_ALLOW_ADVISORY)
    answer, state = hg.guard("grok", GROK_PAYLOAD, str(tmp_path))
    assert answer == GROK_ALLOW_ADVISORY and state == hg.NEUTRAL_UNKNOWN


def test_grok_non_string_additional_context_is_invalid():
    bad = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                  "additionalContext": 3}}
    assert hg._valid_answer("grok", bad) is False


def test_grok_deny_passes_through_as_engine(tmp_path, monkeypatch):
    stub_shim(monkeypatch, GROK_DENY)
    assert hg.guard("grok", GROK_PAYLOAD, str(tmp_path)) == (GROK_DENY, "engine")


def test_grok_invalid_engine_answer_degrades_to_neutral(tmp_path, monkeypatch):
    stub_shim(monkeypatch, {"decision": "deny"})
    assert hg.guard("grok", GROK_PAYLOAD, str(tmp_path)) == ({}, "degraded")
