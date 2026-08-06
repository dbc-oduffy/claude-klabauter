"""test_workday_complete_step1_validate.py — regression suite for
workday-complete-step1-validate.py's fast-test failure classification.

Covers the build-vs-test-failure misclassification defect: `_classify_fast_test_output`
matched the substring "error:" anywhere in the (lowercased) captured output, which is
also a substring of every lowercased Python exception name pytest renders
("AssertionError:" -> "assertionerror:", "JSONDecodeError:" -> "jsondecoderror:", etc).
Any pytest-based test failure was misclassified as a build failure (exit 2, blocking)
instead of a test-only failure (exit 3, fix-quick-or-flag) — the exit-3 branch was
unreachable dead code in practice. Fixed by requiring "error:" NOT be immediately
preceded by a letter (negative lookbehind), which keeps the genuine compiler-diagnostic
shape ("path/file.c:12:5: error: ...") matching while excluding exception names.

Spec backlink: coordinator/bin/workday-complete-step1-validate.py `_classify_fast_test_output`

Also covers `_fail_on_ambiguous_shell_syntax` -- the guard added by the
2026-07-29 debash pass (docs/2026-07-29-debash-residual-sites-spec.md Group B)
when the fast-test exec path switched from `bash -c <cmd>` to a direct
(no-shell) `shlex.split` + subprocess exec. A configured command carrying
shell syntax (pipe/chain/redirect/substitution) can no longer be honored
without a shell, so it must be refused loudly rather than silently handed to
the resolved program as a literal argv token.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess

import pytest

_REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True, text=True, check=True,
).stdout.strip()
_TARGET = os.path.join(_REPO_ROOT, "coordinator", "bin", "workday-complete-step1-validate.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("workday_complete_step1_validate", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def test_assertion_error_classifies_as_test_failure(mod):
    output = "E   AssertionError: assert 1 == 0\n"
    assert mod._classify_fast_test_output(output, 1) == 3


def test_json_decode_error_classifies_as_test_failure(mod):
    output = "json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)\n"
    assert mod._classify_fast_test_output(output, 1) == 3


def test_compiler_diagnostic_classifies_as_build_failure(mod):
    output = "src/foo.c:12:5: error: expected ';' before '}' token\n"
    assert mod._classify_fast_test_output(output, 1) == 2


def test_build_failed_literal_classifies_as_build_failure(mod):
    assert mod._classify_fast_test_output("BUILD FAILED\n", 1) == 2


def test_compilation_terminated_classifies_as_build_failure(mod):
    assert mod._classify_fast_test_output("compilation terminated.\n", 1) == 2


def test_rc_127_classifies_as_build_failure_regardless_of_output(mod):
    assert mod._classify_fast_test_output("command not found\n", 127) == 2
    assert mod._classify_fast_test_output("", 127) == 2


def test_rc_zero_classifies_as_zero(mod):
    assert mod._classify_fast_test_output("", 0) == 0
    assert mod._classify_fast_test_output("AssertionError: whatever\n", 0) == 0


def test_metachar_guard_allows_plain_quoted_command(mod):
    # Must not raise -- ordinary shell-quoting (a quoted sub-argument) is not
    # a shell metacharacter and is exactly the shape shlex.split is meant to
    # parse without a shell.
    mod._fail_on_ambiguous_shell_syntax("pytest -m 'not slow and not integration'")


@pytest.mark.parametrize(
    "bad_cmd",
    [
        "pytest -m a && pytest -m b",
        "pytest -m a || pytest -m b",
        "pytest | tee out.log",
        "pytest -m a; echo done",
        "pytest > out.log",
        "pytest < input.txt",
        "echo `date`",
        "echo $(date)",
    ],
)
def test_metachar_guard_refuses_shell_syntax(mod, bad_cmd):
    with pytest.raises(mod.AmbiguousShellSyntax):
        mod._fail_on_ambiguous_shell_syntax(bad_cmd)
