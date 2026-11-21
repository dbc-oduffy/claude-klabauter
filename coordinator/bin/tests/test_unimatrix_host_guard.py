"""Behavior pins for the Klabauter host-guard client: open answer per host, bounded wall, no child, deny pass-through."""
import importlib.util
import json
import subprocess
import sys
import tempfile
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
NEUTRAL = {"agy": {"decision": "allow"}, "codex": {}, "grok": {}}
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


@pytest.fixture(autouse=True)
def private_temp_root(tmp_path, monkeypatch):
    """The real shim and guards write managed state under gettempdir(); keep it inside tmp_path so the session Temp stays empty."""
    private = tmp_path / "private-temp"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))


@pytest.fixture(autouse=True)
def classifier_never_abandoned(monkeypatch):
    """A real shim or classifier thread outliving its wall keeps running after teardown restores the session Temp and writes into it."""
    monkeypatch.setattr(hg, "CLASSIFY_WALL_SECS", 20.0)


def degrade_records(home):
    log = home / "host-hooks" / hg.DEGRADE_LOG_NAME
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def assert_open(host, answer, state="degraded"):
    """The host's open answer; an agy degrade is an allow, never the bare ask, and carries a non-empty reason."""
    expected = {"decision": "allow"} if host == "agy" and state == "degraded" else NEUTRAL[host]
    assert {k: v for k, v in answer.items() if k != "reason"} == expected
    if host == "agy" and state == "degraded":
        assert isinstance(answer.get("reason"), str) and answer["reason"]
    elif host != "agy":
        assert "reason" not in answer


def stub_shim(monkeypatch, answer=None, run=None):
    """Seam the shim import: guard() calls `run(host, stdin)` on its walled thread, no engine tree needed."""
    fn = run or (lambda host, text: answer)
    monkeypatch.setattr(hg, "_load_shim", lambda root: types.SimpleNamespace(run=fn))


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
def test_import_error_record_names_the_missing_module(tmp_path, monkeypatch, settings_home, host):
    def boom(root):
        raise ModuleNotFoundError("No module named 'gone_mod'", name="gone_mod")

    monkeypatch.setattr(hg, "_load_shim", boom)
    hg.guard(host, CODEX_PAYLOAD, str(tmp_path))
    rec = degrade_records(settings_home)[0]
    assert (rec["cause"], rec["exc_type"], rec["exc_module"]) == ("import_error", "ModuleNotFoundError", "gone_mod")


def test_run_error_record_names_the_exception_type(tmp_path, monkeypatch, settings_home):
    def boom(h, t):
        raise RuntimeError("boom")

    stub_shim(monkeypatch, run=boom)
    hg.guard("codex", CODEX_PAYLOAD, str(tmp_path))
    rec = degrade_records(settings_home)[0]
    assert (rec["cause"], rec["exc_type"]) == ("run_error", "RuntimeError")
    assert "exc_module" not in rec


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
    answer, state = hg.guard("agy", CODEX_PAYLOAD, None)
    assert state == "degraded" and answer["decision"] == "allow"
    assert "DEGRADED (no_engine_root)" in answer["reason"]


def agy_command(command):
    return json.dumps({"toolCall": {"name": "run_command", "args": {"CommandLine": command}},
                       "workspacePaths": ["/x"], "conversationId": "c1"})


@pytest.fixture
def engine_on_path(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO))


def degrade_causes(monkeypatch, tmp_path):
    """One (label, call) per degrade branch of guard(); each call takes the agy stdin and returns (answer, state)."""
    def run_error(text):
        stub_shim(monkeypatch, run=lambda h, t: (_ for _ in ()).throw(RuntimeError("boom")))
        return hg.guard("agy", text, str(tmp_path))

    def invalid_answer(text):
        stub_shim(monkeypatch, "not a dict")
        return hg.guard("agy", text, str(tmp_path))

    def timeout(text):
        release = threading.Event()
        stub_shim(monkeypatch, run=lambda h, t: release.wait(60))
        try:
            return hg.guard("agy", text, str(tmp_path), budget_secs=0.05)
        finally:
            release.set()

    return [("no_engine_root", lambda text: hg.guard("agy", text, None)),
            ("run_error", run_error), ("invalid_answer", invalid_answer), ("timeout", timeout)]


IRREVERSIBLE = ["git clean -fd", "git reset --hard", "git push --force", "git push --force origin topic",
                "git -C sub clean -fdx", "git add . && git reset --hard HEAD~2"]


@pytest.mark.parametrize("command", IRREVERSIBLE)
def test_agy_degrade_denies_irreversible_command_naming_the_cause(tmp_path, monkeypatch, settings_home, engine_on_path, command):
    for cause, call in degrade_causes(monkeypatch, tmp_path):
        answer, state = call(agy_command(command))
        assert state == "degraded", cause
        assert answer["decision"] == "deny", (cause, answer)
        assert "(%s)" % cause in answer["reason"], (cause, answer)
    assert {r["cause"] for r in degrade_records(settings_home)} == {"no_engine_root", "run_error", "invalid_answer", "timeout"}
    assert {r["verdict"] for r in degrade_records(settings_home)} == {"deny"}


@pytest.mark.parametrize("command", ["git status", "git log -3", "git commit -m wip", "git push origin topic",
                                     "git clean -n", "ls", "echo git reset --hard"])
def test_agy_degrade_allows_other_commands_with_a_loud_advisory_and_telemetry(tmp_path, monkeypatch, settings_home, engine_on_path, command):
    for cause, call in degrade_causes(monkeypatch, tmp_path):
        answer, state = call(agy_command(command))
        assert state == "degraded", cause
        assert answer["decision"] == "allow", (cause, answer)
        assert "DEGRADED (%s)" % cause in answer["reason"] and "proceeding open" in answer["reason"], (cause, answer)
    records = degrade_records(settings_home)
    assert len(records) == 4 and {r["verdict"] for r in records} == {"allow"}
    assert all("unchecked" not in r for r in records)


@pytest.mark.parametrize("command", IRREVERSIBLE + ["git status", "ls"])
def test_agy_degrade_never_answers_a_bare_ask(tmp_path, monkeypatch, engine_on_path, command):
    for _cause, call in degrade_causes(monkeypatch, tmp_path):
        answer, _state = call(agy_command(command))
        assert answer["decision"] in ("allow", "deny") and answer.get("reason")
    for text in ("", "not json", "[1]", agy_command("")):
        answer, _state = hg.guard("agy", text, None)
        assert answer["decision"] in ("allow", "deny") and answer.get("reason")


def test_agy_degrade_classification_spawns_nothing(tmp_path, monkeypatch, engine_on_path):
    def no_spawn(*a, **k):
        raise AssertionError("the degrade classification spawned a child process")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    for command in IRREVERSIBLE + ["git status"]:
        answer, _ = hg.guard("agy", agy_command(command), None)
        assert answer["decision"] in ("allow", "deny")


def test_agy_degrade_with_an_unloadable_classifier_allows_and_says_so(tmp_path, monkeypatch, settings_home):
    def boom(root):
        raise ImportError("no backstop")

    monkeypatch.setattr(hg, "_load_backstop", boom)
    answer, state = hg.guard("agy", agy_command("git clean -fd"), None)
    assert state == "degraded" and answer["decision"] == "allow"
    assert "irreversible-class check also unavailable (classifier_unavailable)" in answer["reason"]
    (rec,) = degrade_records(settings_home)
    assert rec["verdict"] == "allow" and rec["unchecked"] == "classifier_unavailable"


def test_agy_degrade_with_a_classifier_timeout_allows_with_the_warning_naming_it(monkeypatch, settings_home):
    release = threading.Event()
    monkeypatch.setattr(hg, "_load_backstop", lambda root: release.wait(60))
    monkeypatch.setattr(hg, "CLASSIFY_WALL_SECS", 0.05)
    try:
        answer, state = hg.guard("agy", agy_command("git clean -fd"), None)
    finally:
        release.set()
    assert state == "degraded" and answer["decision"] == "allow"
    assert answer["reason"] == ("WARNING: Klabauter guard DEGRADED (no_engine_root); irreversible-class check also "
                                "unavailable (classifier_timeout); proceeding open")
    (rec,) = degrade_records(settings_home)
    assert rec["verdict"] == "allow" and rec["unchecked"] == "classifier_timeout"


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
        answer, state = hg.guard(host, payload, str(REPO), budget_secs=20.0)
        assert state in ("neutral-unknown", "degraded"), (host, answer, state)
        assert_open(host, answer, state)
        assert state != "engine"
    assert spawns == []


def test_cold_engine_force_push_to_main_is_denied_and_status_is_not(monkeypatch, settings_home):
    """Real codex and agy payload shapes, warm dial forced to a miss: the backstop answers the deny."""
    monkeypatch.syspath_prepend(str(REPO))
    from coordinator_core.warm import client

    monkeypatch.setattr(client, "try_warm_dispatch", lambda *a, **k: None)
    codex = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "s1", "cwd": "/x",
             "tool_input": {"command": "git push --force origin main"}}
    agy = {"toolCall": {"name": "run_command", "args": {"CommandLine": "git push --force origin main"}},
           "workspacePaths": ["/x"], "conversationId": "c1"}
    answer, state = hg.guard("codex", json.dumps(codex), str(REPO), budget_secs=5.0)
    assert state == "engine" and hg._is_codex_deny(answer), (answer, state)
    answer, state = hg.guard("agy", json.dumps(agy), str(REPO), budget_secs=5.0)
    assert state == "engine" and answer["decision"] == "deny" and answer["reason"], (answer, state)
    codex["tool_input"]["command"] = "git status"
    agy["toolCall"]["args"]["CommandLine"] = "git status"
    answer, state = hg.guard("codex", json.dumps(codex), str(REPO), budget_secs=5.0)
    assert answer == {} and state == "neutral-unknown"
    answer, state = hg.guard("agy", json.dumps(agy), str(REPO), budget_secs=5.0)
    assert answer["decision"] != "deny" and state != "engine"


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


def test_stub_deny_passes_through_untouched(tmp_path, monkeypatch):
    deny = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "no"}}
    stub_shim(monkeypatch, deny)
    assert hg.guard("codex", CODEX_PAYLOAD, str(tmp_path)) == (deny, "engine")
    agy_deny = {"decision": "deny", "reason": "no"}
    stub_shim(monkeypatch, agy_deny)
    assert hg.guard("agy", AGY_PAYLOAD, str(tmp_path)) == (agy_deny, "engine")


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
def test_agy_engine_non_deny_answers_allow_never_a_bare_ask(tmp_path, monkeypatch, decision):
    stub_shim(monkeypatch, {"decision": decision})
    answer, state = hg.guard("agy", AGY_PAYLOAD, str(tmp_path))
    assert state != "engine"
    if decision == "force_ask":
        assert state == "degraded" and answer["decision"] == "allow" and answer["reason"]
    else:
        assert answer == {"decision": "allow"} and state == hg.NEUTRAL_UNKNOWN


@pytest.mark.parametrize("decision", ["allow", "ask"])
def test_agy_engine_non_deny_keeps_the_advisory_reason_as_allow(tmp_path, monkeypatch, decision):
    stub_shim(monkeypatch, {"decision": decision, "reason": "advisory text"})
    answer, state = hg.guard("agy", AGY_PAYLOAD, str(tmp_path))
    assert answer == {"decision": "allow", "reason": "advisory text"} and state == hg.NEUTRAL_UNKNOWN


def test_agy_engine_deny_passes_through_unchanged_as_engine(tmp_path, monkeypatch):
    deny = {"decision": "deny", "reason": "irreversible"}
    stub_shim(monkeypatch, dict(deny))
    answer, state = hg.guard("agy", AGY_PAYLOAD, str(tmp_path))
    assert answer == deny and state == "engine"


def test_agy_advisory_is_stashed_for_preinvocation_while_the_guard_answer_is_an_allow_with_the_reason(tmp_path, monkeypatch):
    # PreToolUse takes no `injectSteps`: delivery is the stash the adapter writes, drained by the
    # PreInvocation hook; the guard answer is an allow carrying the advisory reason.
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.syspath_prepend(str(REPO))
    from coordinator_core.host_hooks.agy_adapter import AgyAdapter, advisory_dir
    rendered = AgyAdapter().render(
        AgyAdapter().adapt_event({"conversationId": "conv-1",
                                  "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}),
        {"decision": "allow", "additionalContext": "advisory nag"})
    assert rendered == {"decision": "allow", "reason": "advisory nag"} and "injectSteps" not in rendered
    assert [p.read_text(encoding="utf-8") for p in (advisory_dir() / "conv-1").glob("*.txt")] == ["advisory nag"]
    stub_shim(monkeypatch, rendered)
    answer, state = hg.guard("agy", AGY_PAYLOAD, str(tmp_path))
    assert answer == {"decision": "allow", "reason": "advisory nag"} and state == hg.NEUTRAL_UNKNOWN


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


# --- multi-file apply_patch through the real shim, adapter and guard validation ---------------------

def _loose_repo(tmp_path, files):
    import hashlib
    import zlib

    def obj(git, kind, payload):
        raw = f"{kind} {len(payload)}".encode() + b"\0" + payload
        sha = hashlib.sha1(raw).hexdigest()
        path = git / "objects" / sha[:2] / sha[2:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(zlib.compress(raw))
        return sha

    root = tmp_path / "repo"
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    body = b""
    for name, text in sorted(files.items()):
        (root / name).write_text(text)
        body += b"100644 " + name.encode() + b"\0" + bytes.fromhex(obj(git, "blob", text.encode()))
    tree = obj(git, "tree", body)
    sha = obj(git, "commit", f"tree {tree}\nauthor a <a@b> 0 +0000\ncommitter a <a@b> 0 +0000\n\nm\n".encode())
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "refs" / "heads" / "main").write_text(sha + "\n")
    return root


def _patch_text(*hunks):
    return "*** Begin Patch\n" + "".join(hunks) + "*** End Patch"


def _add_hunk(name, n):
    return f"*** Add File: {name}\n" + "".join(f"+row {i}\n" for i in range(n))


def _update_hunk(name, removed, added):
    return (f"*** Update File: {name}\n@@\n" + "".join(f"-l{i}\n" for i in range(1, removed + 1))
            + "".join(f"+new{i}\n" for i in range(added)))


@pytest.fixture
def warm_engine_in_process(tmp_path, monkeypatch):
    """The warm dial answered in-process by the same `tool_policy.evaluate` the warm op runs."""
    import tempfile

    import coordinator_core.machine_profile as mp
    from coordinator_core.host_hooks import shim, tool_policy
    from coordinator_core.hooks import _em_write_budget as leaf
    from coordinator_core.hooks import nudge_em_code_dispatch as budget_hook

    sys_tmp = tmp_path / "sys-tmp"
    sys_tmp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sys_tmp))
    for target in (budget_hook, leaf):
        monkeypatch.setattr(target, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
        monkeypatch.setattr(target, "_session_sizing_state", lambda payload, sid: (False, False))
    monkeypatch.setattr(budget_hook, "resolve_mode", lambda key, sid: False)
    monkeypatch.setattr(mp, "guard_level", lambda name: "warn")
    monkeypatch.setattr(shim, "_dial", lambda request: {"result": tool_policy.evaluate(request["params"]["payload"])})
    monkeypatch.setattr(shim, "DIAL_BUDGET_SECS", 10.0)  # the answer under test, not the dial wall, is pinned here
    return _loose_repo(tmp_path, {"a.py": "".join(f"l{i}\n" for i in range(10)),
                                  "b.py": "".join(f"l{i}\n" for i in range(10))})


GUARD_PATCHES = [
    ("add-add", [_add_hunk("bin/wordcount.py", 42), _add_hunk("tests/test_wordcount.py", 74)], "2/2 files, 116/30 lines"),
    ("update-update", [_update_hunk("a.py", 5, 20), _update_hunk("b.py", 5, 20)], "2/2 files, 50/30 lines"),
    ("add-update", [_add_hunk("bin/new.py", 20), _update_hunk("a.py", 5, 20)], "2/2 files, 45/30 lines"),
]


@pytest.mark.parametrize("name,hunks,totals", GUARD_PATCHES, ids=[p[0] for p in GUARD_PATCHES])
def test_multi_file_apply_patch_guard_answer_is_the_budget_advisory_with_true_totals(
        warm_engine_in_process, name, hunks, totals):
    root = warm_engine_in_process
    for session in ("sess-one", "sess-two"):
        for _ in range(2):
            payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "session_id": session,
                                  "cwd": str(root), "tool_input": {"command": _patch_text(*hunks)}})
            answer, state = hg.guard("codex", payload, str(REPO), budget_secs=20.0)
            assert state == "neutral-unknown" and answer != {}, (answer, state)
            context = answer["hookSpecificOutput"]["additionalContext"]
            assert context.count("XS budget spent") == 1 and totals in context, context


GROK_NAG_ANSWER = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                          "additionalContext": "NAG-BODY"}}


def stub_nag_shim(monkeypatch, answer, run=None, nag=("/repo", "sid-n", "NAG-BODY")):
    """A shim stub that records the nag it merged and every `mark_if_delivered` call the guard makes."""
    marks = []
    state = {"nag": None}

    def _run(host, text):
        out = run(host, text) if run else answer
        state["nag"] = nag
        return out

    def _take():
        taken, state["nag"] = state["nag"], None
        return taken

    shim = types.SimpleNamespace(run=_run, take_pending_nag=_take,
                                 mark_if_delivered=lambda pending, ans: marks.append((pending, ans)))
    monkeypatch.setattr(hg, "_load_shim", lambda root: shim)
    return marks


def _main_with(monkeypatch, tmp_path, host="grok"):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(GROK_PAYLOAD))
    return hg.main(["--host", host, "--engine-root", str(tmp_path)])


def test_main_marks_the_nag_after_writing_the_answer_that_carries_it(tmp_path, monkeypatch, capsys):
    marks = stub_nag_shim(monkeypatch, GROK_NAG_ANSWER)
    assert _main_with(monkeypatch, tmp_path) == 0
    assert json.loads(capsys.readouterr().out) == GROK_NAG_ANSWER
    assert marks == [(("/repo", "sid-n", "NAG-BODY"), GROK_NAG_ANSWER)]


def test_main_marks_nothing_after_a_timeout(tmp_path, monkeypatch, capsys):
    release = threading.Event()
    marks = stub_nag_shim(monkeypatch, GROK_NAG_ANSWER, run=lambda h, t: release.wait(30) or GROK_NAG_ANSWER)
    monkeypatch.setattr(hg, "DEFAULT_BUDGET_SECS", 0.1)
    monkeypatch.setattr(hg.guard, "__defaults__", (0.1, None))
    try:
        _main_with(monkeypatch, tmp_path)
    finally:
        release.set()
    assert json.loads(capsys.readouterr().out) == {}
    assert marks == []


def test_main_marks_nothing_when_the_answer_is_degraded_to_neutral(tmp_path, monkeypatch, capsys):
    marks = stub_nag_shim(monkeypatch, {"not": "a decision"})
    _main_with(monkeypatch, tmp_path)
    assert json.loads(capsys.readouterr().out) == {}
    assert marks == []


def test_guard_without_a_pending_box_still_returns_the_pair(tmp_path, monkeypatch):
    stub_nag_shim(monkeypatch, GROK_NAG_ANSWER)
    answer, state = hg.guard("grok", GROK_PAYLOAD, str(tmp_path))
    assert answer == GROK_NAG_ANSWER and state == "neutral-unknown"


COLD_CHILD = (
    "import importlib.util, json, sys, time\n"
    "spec = importlib.util.spec_from_file_location('hg_cold', sys.argv[1])\n"
    "mod = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(mod)\n"
    "answer, state = mod.guard('grok', sys.argv[3], sys.argv[2])\n"
    "print(json.dumps({'answer': answer, 'state': state, 'process_secs': time.process_time()}))\n"
)


def _cold_payload(host, tool, args, cwd):
    if host == "agy":
        return json.dumps({"conversationId": "c1", "toolCall": {"name": tool, "args": args}})
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": args,
                       "session_id": "s1", "cwd": cwd})


@pytest.mark.parametrize("host", ["codex", "agy"])
def test_cold_shell_command_runs_the_chain_and_writes_no_guard_degrade_row(host, tmp_path):
    """A shell command reaches the in-process chain and is allowed: state neutral-unknown, no guard degrade row,
    and agy's answer is not the fail-open reason. A tool the chain does not cover (view_file) answers the fail-open reason."""
    shell = ("run_command", {"CommandLine": "ls"}) if host == "agy" else ("Bash", {"command": "ls"})
    answer, state = hg.guard(host, _cold_payload(host, *shell, str(tmp_path)), str(REPO))
    assert state == "neutral-unknown"
    assert not hg.degrade_log_path().exists()
    assert "guard did not run" not in json.dumps(answer)
    if host == "agy":
        unchained, _ = hg.guard(host, _cold_payload(host, "view_file", {"AbsolutePath": str(tmp_path)}, str(tmp_path)),
                                str(REPO))
        assert "guard did not run" in unchained["reason"]


def allow_rows(home):
    log = home / "host-hooks" / hg.ALLOW_LOG_NAME
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def stub_shim_with_evaluation(monkeypatch, answer, evaluation):
    monkeypatch.setattr(hg, "_load_shim", lambda root: types.SimpleNamespace(
        run=lambda host, text: answer, take_evaluation=lambda: evaluation))


@pytest.mark.parametrize("answer", [{}, CODEX_ADVISORY])
def test_codex_allow_appends_one_evaluated_allow_row(tmp_path, monkeypatch, settings_home, answer):
    stub_shim_with_evaluation(monkeypatch, answer, {"route": "warm", "guards_evaluated": []})
    assert hg.guard("codex", CODEX_PAYLOAD, str(tmp_path)) == (answer, "neutral-unknown")
    (row,) = allow_rows(settings_home)
    assert row["kind"] == "evaluated-allow" and row["host"] == "codex" and row["ts"] > 0
    assert row["route"] == "warm" and row["guards_evaluated"] == []
    assert degrade_records(settings_home) == []


def test_codex_cold_allow_row_names_guards(tmp_path, monkeypatch, settings_home):
    stub_shim_with_evaluation(monkeypatch, {}, {"route": "cold", "guards_evaluated": ["backstop"]})
    hg.guard("codex", CODEX_PAYLOAD, str(tmp_path))
    assert allow_rows(settings_home)[0]["route"] == "cold"
    assert allow_rows(settings_home)[0]["guards_evaluated"] == ["backstop"]


def test_codex_allow_from_engine_without_take_evaluation_is_unreported(tmp_path, monkeypatch, settings_home):
    stub_shim(monkeypatch, {})
    hg.guard("codex", CODEX_PAYLOAD, str(tmp_path))
    (row,) = allow_rows(settings_home)
    assert row["route"] == "unreported" and row["guards_evaluated"] == []


def test_codex_deny_appends_an_evaluated_deny_row_and_degrade_appends_none(tmp_path, monkeypatch, settings_home):
    deny = {"hookSpecificOutput": {**GOOD_INNER}}
    evaluation = {"route": "cold", "guards_evaluated": ["backstop", "destructive-rm"],
                  "verdicts": [{"guard": "backstop", "verdict": "allow"}, {"guard": "destructive-rm", "verdict": "deny"}],
                  "chain": "full", "backstop_class": True}
    stub_shim_with_evaluation(monkeypatch, deny, evaluation)
    assert hg.guard("codex", CODEX_PAYLOAD, str(tmp_path))[1] == "engine"
    (row,) = allow_rows(settings_home)
    assert row["kind"] == "evaluated-deny" and row["verdict"] == "deny" and row["route"] == "cold"
    assert row["verdicts"] == evaluation["verdicts"] and row["chain"] == "full" and row["backstop_class"] is True
    stub_shim_with_evaluation(monkeypatch, {"bogus": 1}, evaluation)
    assert hg.guard("codex", CODEX_PAYLOAD, str(tmp_path))[1] == "degraded"
    assert len(allow_rows(settings_home)) == 1


def test_codex_allow_and_advisory_rows_carry_verdict_and_chain(tmp_path, monkeypatch, settings_home):
    evaluation = {"route": "cold", "guards_evaluated": ["backstop", "guard-disk-fill"],
                  "verdicts": [{"guard": "backstop", "verdict": "allow"}, {"guard": "guard-disk-fill", "verdict": "allow"}],
                  "chain": "full"}
    stub_shim_with_evaluation(monkeypatch, {}, evaluation)
    hg.guard("codex", CODEX_PAYLOAD, str(tmp_path))
    stub_shim_with_evaluation(monkeypatch, CODEX_ADVISORY, evaluation)
    hg.guard("codex", CODEX_PAYLOAD, str(tmp_path))
    allow, advisory = allow_rows(settings_home)
    assert (allow["kind"], allow["verdict"]) == ("evaluated-allow", "allow")
    assert (advisory["kind"], advisory["verdict"]) == ("evaluated-allow", "advisory")
    assert allow["verdicts"] == evaluation["verdicts"] and allow["chain"] == "full"


def test_allow_row_write_failure_never_alters_the_answer(tmp_path, monkeypatch, settings_home):
    stub_shim_with_evaluation(monkeypatch, {}, {"route": "cold", "guards_evaluated": ["backstop"]})
    (settings_home / "host-hooks").mkdir(parents=True)
    (settings_home / "host-hooks" / hg.ALLOW_LOG_NAME).mkdir()
    assert hg.guard("codex", CODEX_PAYLOAD, str(tmp_path)) == ({}, "neutral-unknown")


def test_unreachable_engine_codex_ls_records_a_cold_evaluated_allow(tmp_path, settings_home):
    """Real shim, no warm engine: the production path records route cold naming the backstop only."""
    answer, state = hg.guard("codex", _cold_payload("codex", "Bash", {"command": "ls"}, str(tmp_path)), str(REPO))
    assert (answer, state) == ({}, "neutral-unknown")
    (row,) = allow_rows(settings_home)
    assert row["route"] == "cold" and row["guards_evaluated"] == ["backstop"]
