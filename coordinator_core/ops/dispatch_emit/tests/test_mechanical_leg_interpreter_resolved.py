"""Mechanical engine legs name the engine's resolved interpreter, never a bare PATH `python`, and an overlay
exit 1 that is an interpreter import failure folds as inconclusive, not a test red."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, clean_overlay_verify as cov

_BARE = re.compile(r"(^|[\s'\"`])python3?(\.exe)? -m coordinator_core")


def _call(repo_root: str) -> str:
    return ask_compose._overlay_verify_call(head="h", anchor="a", repo_root=repo_root, run_id="run-1")


def test_overlay_leg_command_is_not_a_bare_path_interpreter(tmp_path):
    call = _call(tmp_path.as_posix())
    assert "clean_overlay_verify --run-id run-1" in call
    assert not _BARE.search(call), call
    assert Path(sys.executable).as_posix() in call


def test_retest_remedy_command_is_not_a_bare_path_interpreter(tmp_path):
    call = ask_compose._invariant_retest_call(head="h", anchor="a", repo_root=str(tmp_path), run_id="run-1")
    assert "retest_targeted --isolated-export --params-file " in call
    assert not _BARE.search(call), call
    assert Path(sys.executable).as_posix() in call


def test_clean_overlay_verify_remedy_names_an_absolute_interpreter(tmp_path):
    for text in (cov.remedy_call("run-1"), cov.remedy_call("run-1", tmp_path)):
        assert not _BARE.search(text), text
        assert text.startswith(("/", '"')) or re.match(r"[A-Za-z]:/", text), text


def test_interpreter_with_whitespace_is_quoted(tmp_path, monkeypatch):
    monkeypatch.setattr(cov, "_gate_python", lambda root: str(tmp_path / "with space" / "python"))
    cmd = ask_compose.engine_module_command("m.x", str(tmp_path), "--a 1")
    assert cmd.startswith('"') and cmd.endswith('python" -m m.x --a 1'), cmd


def _fold(exit_code: int, detail: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    fold = ask_compose._OVERLAY_FOLD_JS.replace(
        "__OVERLAY__", f"Promise.resolve({json.dumps({'exit_code': exit_code, 'detail': detail})})"
    )
    program = (
        "const _USAGE_LIMIT_RE = /zzz/; const _haltOnUsageLimit = () => {}; const log = () => {};\n"
        "(async () => { const _halted = false, _testWanted = true;\n"
        "let _verify = { test: { status: 'pass', tests_failed: 0, summary: 's' } };\n"
        f"{fold}\nprocess.stdout.write(JSON.stringify(_verify.test)); }})();\n"
    )
    done = subprocess.run([node, "-"], input=program, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_module_not_found_exit_one_is_not_a_test_red():
    test = _fold(1, "ModuleNotFoundError: No module named 'coordinator_core'")
    assert test["status"] == "pass" and test["tests_failed"] == 0


def test_no_module_named_exit_one_is_not_a_test_red():
    assert _fold(1, "/usr/bin/python: No module named coordinator_core")["status"] == "pass"


def test_real_overlay_red_still_folds_as_a_test_failure():
    test = _fold(1, "overlay verdict: red")
    assert test["status"] == "fail" and test["tests_failed"] == 1


def test_overlay_exit_two_stays_inconclusive():
    assert _fold(2, "overlay verdict: inconclusive")["status"] == "pass"
