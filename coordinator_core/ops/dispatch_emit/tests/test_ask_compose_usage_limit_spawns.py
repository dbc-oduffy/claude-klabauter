"""The ask script turns a usage-limit failure at the terminal test agent into a resumable halt."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.cadence
def test_helper_halts_on_limit_and_rethrows_other_errors(monkeypatch):
    helper = ask_compose._usage_limit_helper_js()
    js = (
        "let _halted = null; const _runId = 'run-1';\n" + helper + "\n"
        "try { throw new Error(\"You've hit your session limit - resets 3:00pm\"); } catch (e) { _haltOnUsageLimit(e); }\n"
        "if (_halted.halted !== 'usage_limit' || _halted.run_id !== 'run-1') throw new Error('no halt');\n"
        "_halted = null;\n"
        "let rethrown = false;\n"
        "try { try { throw new Error('boom'); } catch (e) { _haltOnUsageLimit(e); } } catch { rethrown = true; }\n"
        "if (!rethrown || _halted) throw new Error('boom swallowed');\n"
    )
    r = subprocess.run(
        ["node", "-"], input=js, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert r.returncode == 0, r.stderr


_WEEKLY = "You've hit your weekly limit - resets Nov 17, 3am (Europe/London)"


def _node(js: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["node", "-"], input=js, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.cadence
def test_weekly_limit_halt_carries_parsed_reset_and_refire_after_it():
    js = (
        "let _halted = null; const _runId = 'run-1';\n" + ask_compose._usage_limit_helper_js() + "\n"
        f"_haltOnUsageLimit(new Error({_WEEKLY!r}));\n"
        "if (_halted.halted !== 'usage_limit') throw new Error('class ' + _halted.halted);\n"
        "if (_halted.reset !== 'Nov 17, 3am') throw new Error('reset ' + _halted.reset);\n"
        "if (_halted.reset_zone !== 'Europe/London') throw new Error('zone ' + _halted.reset_zone);\n"
        "const n = _halted.next_action;\n"
        "if (!n.startsWith('After the limit resets (Nov 17, 3am Europe/London)') || !n.includes('resumeFromRunId')) "
        "throw new Error('next_action ' + n);\n"
        "_halted = null; _haltOnUsageLimit(new Error('quota exceeded'));\n"
        "if (_halted.reset !== null || !_halted.next_action.startsWith('After the limit resets,')) "
        "throw new Error('no-reset shape ' + JSON.stringify(_halted));\n"
    )
    r = _node(js)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.cadence
def test_empty_test_verdict_with_limit_text_halts_usage_limit_and_plain_empty_still_does_not():
    helper = ask_compose._usage_limit_helper_js()
    block = (
        ask_compose._test_verdict_limit_route_js() + "\n" + ask_compose._test_verdict_empty_halt_js(None)
    ).replace("TEST_GUARD", "true")

    def run(result_js: str) -> subprocess.CompletedProcess:
        return _node(
            "let _halted = null; const _runId = 'run-1'; let _testResult = " + result_js + ";\n"
            f"const {ask_compose._TEST_RAN_FN} = () => {ask_compose._TEST_RAN_JS};\n"
            "const _refire = () => 'refire'; const _sizingRel = 's.yaml';\n"
            + helper + "\nfunction _t() {\n" + block + "\n}\n_t();\n"
            "console.log(_halted.halted);\n"
        )

    limit = run("{ status: 'not_run', summary: " + repr(_WEEKLY) + " }")
    assert limit.returncode == 0, limit.stderr
    assert limit.stdout.strip() == "usage_limit"
    plain = run("{ status: 'not_run', summary: 'no sidecar' }")
    assert plain.stdout.strip() == "test_verdict_empty", plain.stderr


def _verify_tests_throw_run(throw_js: str) -> subprocess.CompletedProcess:
    """Eval the composed verify-tests catch ahead of the empty-verdict halt; prints the resulting halt kind.

    The outer `_runVerify` catch is a hand-written replica of the one the composer emits, so only the inner
    verify-tests catch, the limit route and the empty-verdict halt are the real composed text."""
    script = ask_compose._usage_limit_helper_js()
    catch_js = (
        "const log = () => {}; async function _runVerify() { try { try { throw " + throw_js + "; } "
        + ask_compose._verify_tests_catch_js().replace(ask_compose._VERIFY_TEST_RESULT_VAR, "_testResult") + " "
        "} catch (e) { const _m = _errLimitText(e) ?? String((e && e.message) ?? e); "
        "if (_USAGE_LIMIT_RE.test(_m)) { _haltOnUsageLimit(e); } } }\n"
    )
    block = (
        ask_compose._test_verdict_limit_route_js() + "\n" + ask_compose._test_verdict_empty_halt_js(None)
    ).replace("TEST_GUARD", "true")
    return _node(
        "let _halted = null; const _runId = 'run-1'; let _testResult = null;\n"
        "const _refire = () => 'refire'; const _sizingRel = 's.yaml';\n"
        + script + "\n" + catch_js
        + "(async () => { await _runVerify();\nfunction _t() {\n" + block + "\n}\n_t();\n"
        "console.log(_halted.halted + '|' + _halted.next_action); })();\n"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.cadence
def test_verify_tests_refusal_carried_only_by_failure_cause_halts_usage_limit():
    limit = _verify_tests_throw_run(
        "Object.assign(new Error('agent returned no structured output'), { cause: new Error(" + repr(_WEEKLY) + ") })"
    )
    assert limit.returncode == 0, limit.stderr
    kind, _, action = limit.stdout.strip().partition("|")
    assert kind == "usage_limit"
    assert "resumeFromRunId" in action
    raw = _verify_tests_throw_run("Object.assign(new Error('no output'), { output: " + repr(_WEEKLY) + " })")
    assert raw.stdout.strip().startswith("usage_limit|"), raw.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.cadence
def test_verify_tests_non_limit_failure_still_halts_test_verdict_empty():
    plain = _verify_tests_throw_run(
        "Object.assign(new Error('agent returned no structured output'), { cause: new Error('socket hang up') })"
    )
    assert plain.returncode == 0, plain.stderr
    assert plain.stdout.strip().startswith("test_verdict_empty|"), plain.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.cadence
def test_limit_regex_matches_dated_reset_form():
    js = (
        f"const re = {ask_compose._USAGE_LIMIT_RE};\n"
        f"if (!re.test({_WEEKLY!r})) throw new Error('weekly');\n"
        "if (!re.test('resets Nov 17, 3am')) throw new Error('dated');\n"
        "if (re.test('the test failed on line 4')) throw new Error('false positive');\n"
    )
    r = _node(js)
    assert r.returncode == 0, r.stderr
