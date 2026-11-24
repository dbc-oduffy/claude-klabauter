"""Pure tests for ops/_anchor_check.py."""

import os
import subprocess

import pytest

pytestmark = pytest.mark.complete

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


@pytest.mark.parametrize(
    "cmd",
    ["grep -n foo a.txt", "grep -cn foo a.txt", "grep -nc foo a.txt", "grep -c -n foo a.txt",
     "grep -nE 'foo' a.txt", "grep -n -e foo a.txt"],
)
def test_line_number_flag_is_evaluated_not_skipped(root, cmd):
    r = evaluate(cmd, "3", root)
    assert r["state"] == "MATCH" and r["observed"] == "3"
    assert evaluate(cmd, "2", root)["state"] == "MISMATCH"


@pytest.mark.parametrize(
    "cmd",
    ["grep -ci baz a.txt", "grep -c -i baz a.txt", "grep -ic baz a.txt", "grep -cin baz a.txt",
     "grep -ni baz a.txt", "grep -ciE 'BAZ' a.txt", "grep -ciF baz a.txt",
     "grep -ci -e BAZ -e nomatch a.txt"],
)
def test_ignore_case_counts_case_insensitively(root, cmd):
    assert evaluate(cmd, "1", root)["state"] == "MATCH"
    assert evaluate(cmd, "0", root)["state"] == "MISMATCH"


def test_case_sensitive_stays_case_sensitive(root):
    assert evaluate("grep -c baz a.txt", "0", root)["state"] == "MATCH"
    assert evaluate("grep -ci foo a.txt", "3", root)["state"] == "MATCH"


@pytest.mark.parametrize("cmd", ["grep -cii baz a.txt", "grep -ci -i baz a.txt", "grep -cEi -i baz a.txt"])
def test_duplicate_ignore_case_refused(root, cmd):
    assert _state(root, cmd, "1") == "NOT_EVALUABLE"


def test_line_number_flag_does_not_admit_other_flags(root):
    assert _state(root, "grep -nn foo a.txt", "3") == "NOT_EVALUABLE"
    assert _state(root, "grep foo a.txt", "3") == "NOT_EVALUABLE"


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


def test_zero_count_does_not_stop_chain(root):
    r = evaluate("grep -c nomatch a.txt && grep -c one b.txt", "0 then 1", root)
    assert r["state"] == "MATCH" and r["observed"] == "0\n1"
    assert _state(root, "grep -c nomatch a.txt && grep -c one b.txt", "0") == "MISMATCH"


@pytest.mark.parametrize("sep", ["&&", ";"])
def test_three_file_absence_premise(root, sep):
    (root / "z1.txt").write_text("x\n", encoding="utf-8")
    cmd = f"grep -c nomatch a.txt {sep} grep -c nomatch b.txt {sep} grep -c nomatch z1.txt"
    r = evaluate(cmd, "0 then 0 then 0", root)
    assert r["state"] == "MATCH" and r["observed"] == "0\n0\n0"
    bad = f"grep -c nomatch a.txt {sep} grep -c one b.txt {sep} grep -c nomatch z1.txt"
    r = evaluate(bad, "0 then 0 then 0", root)
    assert r["state"] == "MISMATCH" and r["observed"] == "0\n1\n0"
    assert "b.txt" in r["detail"] and "a.txt" not in r["detail"]
    assert evaluate(bad, "0 then 1 then 0", root)["state"] == "MATCH"


@pytest.mark.parametrize("sep", ["&&", ";"])
def test_absence_premise_names_missing_file(root, sep):
    cmd = f"grep -c nomatch a.txt {sep} grep -c nomatch gone.txt"
    assert evaluate(cmd, "0 then 0", root)["state"] == "MATCH"
    r = evaluate(cmd, "0 then >=1", root)
    assert r["state"] == "MISMATCH" and "gone.txt" in r["detail"] and r["observed"] == "0\n0"
    r = evaluate(cmd, "0", root)
    assert r["state"] == "MISMATCH" and "gone.txt" in r["detail"]


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


def test_file_over_one_mib_is_evaluated(root):
    line = "pad " * 24 + "\n"
    body = line * 12000 + "needle\nneedle\r\nlast needle"
    (root / "huge.py").write_text(body, encoding="utf-8", newline="")
    assert (root / "huge.py").stat().st_size > 1 << 20
    r = evaluate("grep -c needle huge.py", "3", root)
    assert r["state"] == "MATCH" and r["observed"] == "3"
    assert _state(root, "grep -c needle huge.py", "2") == "MISMATCH"
    assert _state(root, "grep -cE 'needle$' huge.py", "3") == "MATCH"
    assert not hasattr(_anchor_check, "MAX_BYTES")


@pytest.mark.parametrize("cmd", [
    "grep -c foo nope.txt", "grep -c -F foo nope.txt", "grep -c -E 'foo|bar' nope.txt",
    'grep -c -e "" nope.txt',
])
def test_missing_path_absence_claim_holds(root, cmd):
    r = evaluate(cmd, "0", root)
    assert r["state"] == "MATCH" and r["observed"] == "0"
    assert evaluate(cmd, "<1", root)["state"] == "MATCH"


@pytest.mark.parametrize("expect", ["1", ">=1", ">0"])
def test_missing_path_with_positive_expect_stays_mismatch(root, expect):
    r = evaluate("grep -c -F foo nope.txt", expect, root)
    assert r["state"] == "MISMATCH" and "nope.txt" in r["detail"]
    assert "no such file" in r["detail"]


def test_missing_path_at_ref_absence_holds_and_positive_expect_mismatches(root):
    # The relayed shape: `git grep -c -e "" HEAD -- <missing>` expect 0.
    cmd = 'git grep -c -e "" HEAD -- gone.py'
    blobs = {("HEAD", "gone.py"): None}
    r = evaluate(cmd, "0", root, None, blobs)
    assert r["state"] == "MATCH" and r["observed"] == "0"
    r = evaluate(cmd, ">=1", root, None, blobs)
    assert r["state"] == "MISMATCH" and "no such file at HEAD: gone.py" in r["detail"]


def test_ref_less_git_grep_stays_not_evaluable_and_grammar_teaches_the_form(root):
    cmd = "git grep -c PAT -- a.txt"
    r = evaluate(cmd, "0", root)
    assert r["state"] == "NOT_EVALUABLE" and "<ref> -- <path>" in r["detail"]
    assert _anchor_check.evaluable(cmd) is not None
    assert _anchor_check.evaluable("git grep -c PAT HEAD -- a.txt") is None
    assert _anchor_check.evaluable("grep -c -F PAT a.txt") is None
    g = _anchor_check.GRAMMAR
    assert "grep -c [-F|-E] PAT FILE" in g and "REF -- FILE" in g and "expect 0" in g
    assert "\n" not in g


@pytest.mark.parametrize(
    "operand", ["../a.txt", "sub/../a.txt", "*.txt", "a.tx?", "a.[tx]xt", "x:y.txt", "sub\\c.py", "/etc/hosts"]
)
def test_bad_operands(root, operand):
    assert _state(root, f"grep -c foo '{operand}'", "1") == "NOT_EVALUABLE"


def test_absolute_operand(root):
    assert _state(root, f"grep -c foo {root / 'a.txt'}", "3") == "NOT_EVALUABLE"


@pytest.mark.parametrize("operand", ["~/.claude/settings.json", "~other/x.txt", "~"])
def test_home_operand_is_not_evaluable_never_a_missing_file(root, operand):
    # A `~/` operand once joined under the repo root, read as a missing file and became MISMATCH.
    assert _state(root, f"grep -c accept {operand}", "1") == "NOT_EVALUABLE"


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
     "grep -cv foo a.txt", "grep -cw foo a.txt", "grep -cx foo a.txt",
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


@pytest.mark.parametrize(
    "cmd,want",
    [
        ("grep -c a...b f", "a...b"),
        ("grep -c -E a...b f", "a...b"),
        ("grep -ci a...b f", "a...b"),
        ("git grep -c a...b HEAD -- f", "a...b"),
        ("grep -c -e ok -e x...y f", "x...y"),
        ("grep -c ok f && grep -c a...b g", "a...b"),
        ("grep -c a....b f", "a....b"),
        ("grep -c -F a...b f", None),
        ("grep -c 'a\\.\\.\\.b' f", None),
        ('grep -c "a\\.\\.\\.b" f', None),
        ("grep -c 'a\\...b' f", None),
        ("grep -c a\\.\\.\\.b f", "a...b"),
        ("grep -c a.*b f", None),
        ("grep -c a..b f", None),
        ("grep -c -F -e a...b f", None),
        ("find . | wc -l", None),
    ],
)
def test_elided_pattern(cmd, want):
    assert _anchor_check.elided_pattern(cmd) == want


def test_grammar_names_grep_c():
    assert "grep -c" in _anchor_check.GRAMMAR
    assert "\n" not in _anchor_check.GRAMMAR


def test_evaluable_git_grep_and_refuses_git_show():
    assert _anchor_check.evaluable("git grep -c -F PAT origin/main -- a.py") is None
    assert _anchor_check.evaluable("git grep -c -e A -e B HEAD -- a.py") is None
    assert _anchor_check.evaluable("git show origin/main:a.py") == "only grep stages are evaluable"


def test_parse_stage_git_grep_tuple():
    assert _anchor_check._parse_stage(["git", "grep", "-cF", "P", "HEAD", "--", "a.py"]) == (
        "F", ["P"], "a.py", "HEAD")
    assert _anchor_check._parse_stage(["grep", "-c", "P", "a.py"]) == ("B", ["P"], "a.py", None)


@pytest.mark.parametrize(
    "cmd",
    ["git grep -c PAT a.py", "git grep -c PAT -- a.py", "git grep -c PAT 'a b' -- a.py",
     "git grep -c PAT ../x -- a.py", "git grep -c PAT -x -- a.py", "git grep -c PAT HEAD:a.py -- a.py",
     "git grep PAT HEAD -- a.py", "git grep -c PAT HEAD -- a.py b.py"],
)
def test_git_grep_refusals(cmd):
    assert _anchor_check.evaluable(cmd) is not None


def test_count_bytes_matches_file_rule(root):
    regexes = [_anchor_check._compile("F", "x")]
    data = (root / "crlf.txt").read_bytes()
    assert _anchor_check._count_bytes(regexes, data) == _anchor_check._count(regexes, root / "crlf.txt") == 2
    assert _anchor_check._count_bytes(regexes, b"") == 0
    assert _anchor_check._count_bytes(regexes, b"x") == 1


def test_count_over_bound_is_not_evaluable_not_a_hang(root, monkeypatch):
    monkeypatch.setattr(_anchor_check, "MAX_LANE_BYTES", 64)
    (root / "big.txt").write_bytes(b"foo\n" * 100)
    got = evaluate("grep -c foo big.txt", "100", root)
    assert got["state"] == "NOT_EVALUABLE" and "bound" in got["detail"]
    assert evaluate("grep -c foo a.txt", ">=1", root)["state"] == "MATCH"


def test_cap_charges_bytes_read_so_a_decided_big_file_still_evaluates(root, monkeypatch):
    monkeypatch.setattr(_anchor_check, "MAX_LANE_BYTES", 64)
    (root / "big.txt").write_bytes(b"foo\n" * 100)
    got = evaluate("grep -c foo big.txt", ">=1", root)
    assert got["state"] == "MATCH" and got["observed"] == ">=1"


def test_cap_is_shared_across_the_lane_stages(root, monkeypatch):
    monkeypatch.setattr(_anchor_check, "MAX_LANE_BYTES", 150)
    (root / "big.txt").write_bytes(b"foo\n" * 30)
    cmd = "grep -c foo big.txt && grep -c foo big.txt"
    assert evaluate(cmd, "30 then 30", root)["state"] == "NOT_EVALUABLE"
    assert evaluate("grep -c foo big.txt", "30", root)["state"] == "MATCH"


def test_short_circuited_count_is_reported_as_a_lower_bound(root):
    got = evaluate("grep -c foo a.txt", ">=1", root)
    assert got["state"] == "MATCH" and got["observed"] == ">=1"
    got = evaluate("grep -c foo a.txt", ">1", root)
    assert got["state"] == "MATCH" and got["observed"] == ">=2"
    miss = evaluate("grep -c foo a.txt && grep -c nomatch b.txt", ">=1 then >=2", root)
    assert miss["state"] == "MISMATCH" and miss["observed"] == ">=1\n0"
    assert "b.txt counted 0" in miss["detail"]
    exact = evaluate("grep -c foo a.txt", "3", root)
    assert exact["observed"] == "3"
    assert evaluate("grep -c foo a.txt", "<=2", root)["observed"] == "3"


def test_large_ascii_file_counts_fast_and_short_circuits(root):
    import time

    (root / "big.txt").write_bytes(b"alpha beta gamma\n" * 200_000 + b"needle\n")
    t0 = time.process_time()
    assert evaluate("grep -c needle big.txt", "1", root)["state"] == "MATCH"
    assert evaluate("grep -c alpha big.txt", ">=3", root)["observed"] == ">=3"
    assert time.process_time() - t0 < 0.5


def test_fast_path_agrees_with_decoded_path_on_non_ascii(root):
    (root / "u.txt").write_bytes("caf\u00e9 x\nabc\n\xff\xfe x\n".encode("latin-1", "replace"))
    (root / "u8.txt").write_bytes("a\u00e9b\nacb\nab\n".encode("utf-8"))
    assert evaluate("grep -c a.b u8.txt", "2", root)["state"] == "MATCH"
    assert evaluate("grep -c -e a.b -e zzz u8.txt", "2", root)["state"] == "MATCH"
    assert evaluate("grep -c ab u8.txt", "1", root)["state"] == "MATCH"
    assert evaluate("grep -c x u.txt", "2", root)["state"] == "MATCH"
