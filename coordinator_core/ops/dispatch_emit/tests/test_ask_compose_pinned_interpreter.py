"""The ask verify/ledger-check test instruction carries the target repo's pinned interpreter, and the verdict params carry tests_run."""

from __future__ import annotations

import sys

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import _TARGETED_TESTS_JS, _test_instruction_js
from coordinator_core.ops.dispatch_emit.test_interpreter import PORTABLE_CLAUSE


def _repo(tmp_path, frontmatter: str) -> str:
    (tmp_path / "coordinator.local.md").write_text(f"---\n{frontmatter}\n---\n", encoding="utf-8")
    return str(tmp_path)


def test_pinned_interpreter_reaches_the_test_instruction(tmp_path):
    pin = sys.executable
    root = _repo(tmp_path, f'targeted_test_python: "{pin}"')
    text = _test_instruction_js("HEAD", root)
    assert pin.replace("\\", "\\\\") in text or pin in text
    assert "falling back to" not in text


def test_no_pin_keeps_the_portable_text(tmp_path):
    root = _repo(tmp_path, 'fast_test_cmd: "python3 -m pytest"')
    assert PORTABLE_CLAUSE in _test_instruction_js("HEAD", root)


def test_unresolvable_pin_names_the_defect(tmp_path):
    root = _repo(tmp_path, 'targeted_test_python: "no-such-interpreter-xyz"')
    text = _test_instruction_js("HEAD", root)
    assert "no-such-interpreter-xyz" in text
    assert "interpreter-resolution-defect:" in text


def test_default_repo_root_is_the_portable_text():
    assert PORTABLE_CLAUSE in _test_instruction_js("HEAD")


def test_targeted_tests_params_carry_tests_run():
    assert "tests_run: _testResult.tests_run ?? null" in _TARGETED_TESTS_JS
    assert ask_compose._TARGETED_TESTS_JS is _TARGETED_TESTS_JS
