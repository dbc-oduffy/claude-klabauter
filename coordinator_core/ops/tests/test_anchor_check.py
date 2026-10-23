"""Pure tests for ops/_anchor_check.py."""

import os
import subprocess

import pytest

from coordinator_core.ops import _anchor_check
from coordinator_core.ops._anchor_check import evaluate


@pytest.fixture
def root(tmp_path):
    (tmp_path / "a.txt").write_text("foo\nbar foo\nBaz\nfoo(1)\na|b\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("one\ntwo\n", encoding="utf-8")
    (tmp_path / "crlf.txt").write_bytes(b"x\r\nx\r\ny\r\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.py").write_text("def evaluate_typed\n", encoding="utf-8")
    return tmp_path


def _state(root, cmd, expect):
    return evaluate(cmd, expect, root)["state"]


@pytest.mark.parametrize(
    "cmd",
    ["grep -c foo a.txt", "grep -cE foo a.txt", "grep -cF foo a.txt", "grep -Ec foo a.txt",
     "grep -c -E foo a.txt", 'grep -c "foo" a.txt'],
)
def test_flag_forms_match(root, cmd):
    r = evaluate(cmd, "3", root)
    assert r["state"] == "MATCH" and r["observed"] == "3"


def test_multiple_e_patterns_count_lines_matching_any(root):
    r = evaluate("grep -c -e foo -e Baz a.txt", "4", root)
    assert r["state"] == "MATCH" and r["observed"] == "4"
    assert evaluate("grep -c -e one a.txt", "0", root)["state"] == "MATCH"
    assert _state(root, "grep -c -e foo -e Baz a.txt", "3") == "MISMATCH"


@pytest.mark.parametrize(
    "cmd", ["grep -c -e foo", "grep -c -e foo -e", "grep -c -e foo bar a.txt", "grep -c -e foo ../a.txt"]
)
def test_e_patterns_still_refuse_bad_operands(root, cmd):
    assert _state(root, cmd, "1") == "NOT_EVALUABLE"


def test_mismatch(root):
    r = evaluate("grep -c foo a.txt", "2", root)
    assert r["state"] == "MISMATCH" and r["observed"] == "3"


def test_two_stage_chain_then(root):
    r = evaluate("grep -c foo a.txt && grep -c one b.txt", "3 then 1", root)
    assert r["state"] == "MATCH" and r["observed"] == "3\n1"
    assert _state(root, "grep -c foo a.txt && grep -c one b.txt", "3 then 2") == "MISMATCH"
    assert _state(root, "grep -c foo a.txt && grep -c one b.txt", "3") == "MISMATCH"


def test_zero_count_stops_chain(root):
    r = evaluate("grep -c nomatch a.txt && grep -c one b.txt", "0", root)
    assert r["state"] == "MATCH" and r["observed"] == "0"
    assert _state(root, "grep -c nomatch a.txt && grep -c one b.txt", "0 then 1") == "MISMATCH"


@pytest.mark.parametrize(
    "expect,ok",
    [("3", True), (">=3", True), (">=4", False), ("<=3", True), ("<=2", False),
     (">2", True), (">3", False), ("<4", True), ("<3", False)],
)
def test_expect_operators(root, expect, ok):
    assert (_state(root, "grep -c foo a.txt", expect) == "MATCH") is ok


def test_bad_expect(root):
    assert _state(root, "grep -c foo a.txt", "three") == "NOT_EVALUABLE"
    assert _state(root, "grep -c foo a.txt", "") == "NOT_EVALUABLE"


def test_crlf(root):
    assert evaluate("grep -c x crlf.txt", "2", root)["state"] == "MATCH"
    assert evaluate("grep -cE 'x$' crlf.txt", "2", root)["state"] == "MATCH"


def test_byte_cap(root, monkeypatch):
    monkeypatch.setattr(_anchor_check, "MAX_BYTES", 5)
    r = evaluate("grep -c foo a.txt", "3", root)
    assert r["state"] == "NOT_EVALUABLE" and "over" in r["detail"]


def test_missing_file_is_mismatch(root):
    r = evaluate("grep -c foo nope.txt", "0", root)
    assert r["state"] == "MISMATCH" and r["observed"] == ""


@pytest.mark.parametrize(
    "operand", ["../a.txt", "sub/../a.txt", "*.txt", "a.tx?", "a.[tx]xt", "x:y.txt", "sub\\c.py", "/etc/hosts"]
)
def test_bad_operands(root, operand):
    assert _state(root, f"grep -c foo '{operand}'", "1") == "NOT_EVALUABLE"


def test_absolute_operand(root):
    assert _state(root, f"grep -c foo {root / 'a.txt'}", "3") == "NOT_EVALUABLE"


def test_symlink_escape(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "o.txt"
    outside.write_text("foo\n")
    try:
        os.symlink(outside, root / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert _state(root, "grep -c foo link.txt", "1") == "NOT_EVALUABLE"


@pytest.mark.parametrize(
    "cmd",
    ["find sub -name c.py | wc -l", "grep -c foo a.txt | wc -l", "grep -c foo a.txt b.txt",
     "grep -ci foo a.txt", "grep -cv foo a.txt", "grep -cw foo a.txt", "grep -cx foo a.txt",
     "grep -cr foo sub", "grep -cl foo a.txt", "grep -ce foo a.txt", "grep -cEF foo a.txt",
     "grep foo a.txt", "grep -c foo a.txt; echo hi", "grep -c foo a.txt || true",
     "grep -c foo a.txt > out.txt", "cat a.txt", "", "grep -c 'unterminated a.txt"],
)
def test_refused_forms(root, cmd):
    assert _state(root, cmd, "1") == "NOT_EVALUABLE"


def test_fixed_vs_regex_vs_bre(root):
    assert evaluate("grep -cF 'foo(1)' a.txt", "1", root)["state"] == "MATCH"
    assert evaluate("grep -cF 'a|b' a.txt", "1", root)["state"] == "MATCH"
    assert evaluate("grep -cE 'foo|Baz' a.txt", "4", root)["state"] == "MATCH"
    assert evaluate("grep -c 'a|b' a.txt", "1", root)["state"] == "MATCH"
    assert evaluate("grep -c 'foo\\|Baz' a.txt", "4", root)["state"] == "MATCH"
    assert evaluate("grep -c 'foo(1)' a.txt", "1", root)["state"] == "MATCH"
    assert evaluate("grep -c 'foo\\(1\\)' a.txt", "0", root)["state"] == "MATCH"
    assert evaluate("grep -c '^foo\\.*' a.txt", "2", root)["state"] == "MATCH"


def test_posix_class_and_backslash_class_refused(root):
    assert _state(root, "grep -cE '[[:alpha:]]+' a.txt", "5") == "NOT_EVALUABLE"
    assert _state(root, "grep -c '[[:alpha:]]' a.txt", "5") == "NOT_EVALUABLE"
    assert _state(root, "grep -c '\\w' a.txt", "5") == "NOT_EVALUABLE"


def test_nested_quantifier_refused(root):
    assert _state(root, "grep -cE '(a+)+$' a.txt", "0") == "NOT_EVALUABLE"
    assert _state(root, "grep -cE '(foo|bar)*' a.txt", "0") != "NOT_EVALUABLE"


@pytest.mark.parametrize("pat", ["(a|aa)+$", "(a|a)*b", "(a|aa){2,}$", "(a|.)*b", "(ab|a)+c"])
def test_overlapping_alternation_refused_fast(root, pat):
    import time

    (root / "big.txt").write_text("a" * 100000 + "\n", encoding="utf-8")
    start = time.perf_counter()
    assert _state(root, f"grep -cE '{pat}' big.txt", "0") == "NOT_EVALUABLE"
    assert time.perf_counter() - start < 0.5


def test_distinct_literal_alternation_stays_evaluable(root):
    assert _state(root, "grep -cE '(foo|bar)+' a.txt", "3") == "MATCH"


def test_bad_regex(root):
    assert _state(root, "grep -cE 'foo[' a.txt", "0") == "NOT_EVALUABLE"


def test_run_receipt_shapes(root):
    (root / "sub" / "x.py").write_text("alpha beta\nalpha\n", encoding="utf-8")
    for expect, cmd in [
        ("2", 'grep -c "alpha" sub/x.py'),
        ("1", 'grep -c "beta" sub/x.py'),
        ("0", 'grep -c "gamma" sub/x.py'),
        ("1", 'grep -c "def evaluate_typed" sub/c.py'),
        ("2", 'grep -cE "alpha( beta)?" sub/x.py'),
    ]:
        assert evaluate(cmd, expect, root)["state"] == "MATCH", cmd
    assert _state(root, "find sub -name x.py | wc -l", "1") == "NOT_EVALUABLE"


def test_zero_spawn(root, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn attempted")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(os, "system", boom)
    assert evaluate("grep -c foo a.txt && grep -c one b.txt", "3 then 1", root)["state"] == "MATCH"
    assert evaluate("find . | wc -l", "1", root)["state"] == "NOT_EVALUABLE"


def test_evaluable_accepts_grep_stages():
    assert _anchor_check.evaluable("grep -c foo a.txt") is None
    assert _anchor_check.evaluable("grep -c foo a.txt && grep -c one b.txt") is None


def test_evaluable_refuses_with_detail():
    assert _anchor_check.evaluable("ls -d bin | wc -l") == "unsupported shell operator '|'"
    assert _anchor_check.evaluable("wc -l x") == "only grep stages are evaluable"


def test_grammar_names_grep_c():
    assert "grep -c" in _anchor_check.GRAMMAR
    assert "\n" not in _anchor_check.GRAMMAR
