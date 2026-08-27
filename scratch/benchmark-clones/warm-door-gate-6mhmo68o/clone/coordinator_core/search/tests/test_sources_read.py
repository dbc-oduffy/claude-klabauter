"""Unit tests for coordinator_core.search.sources_read.

Covers parse-and-produce for the three accepted shapes (cat, head/tail,
sed -n range) and, more importantly, the decline set: everything this module
must refuse rather than approximate. No subprocess -- the differential oracle
against real cat/head/tail/sed lives in C4.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.search import sources_read as sr
from coordinator_core.search.engine import MAX_RENDER_BYTES, Unanswerable


@pytest.fixture()
def workdir(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", newline="")
    (tmp_path / "b.txt").write_text("four\nfive", newline="")  # no trailing newline
    (tmp_path / "empty.txt").write_text("", newline="")
    return str(tmp_path)


def _produce(tokens, cwd):
    return sr.parse_read_segment(tokens).produce(cwd)


# --------------------------------------------------------------------------- cat


def test_cat_single_file(workdir):
    assert _produce(["cat", "a.txt"], workdir) == "one\ntwo\nthree\n"


def test_cat_concatenation_no_headers(workdir):
    # Real `cat` prints no separator/header between files -- that's head/tail.
    assert _produce(["cat", "a.txt", "b.txt"], workdir) == "one\ntwo\nthree\nfour\nfive"


def test_cat_preserves_missing_final_newline(workdir):
    assert _produce(["cat", "b.txt"], workdir) == "four\nfive"


def test_cat_absolute_operand_passthrough(workdir):
    abs_path = os.path.join(workdir, "a.txt")
    assert _produce(["cat", abs_path], workdir) == "one\ntwo\nthree\n"


def test_cat_relative_operand_joins_cwd(workdir):
    sub = os.path.join(workdir, "sub")
    os.mkdir(sub)
    (open(os.path.join(sub, "c.txt"), "w")).write("x")
    assert _produce(["cat", "c.txt"], sub) == "x"


def test_cat_declines_any_flag(workdir):
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["cat", "-n", "a.txt"])


def test_cat_declines_stdin_dash(workdir):
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["cat", "-"])


def test_cat_declines_absent_operand():
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["cat"])


# --------------------------------------------------------------------------- head/tail


def test_head_default_n10(workdir):
    with open(os.path.join(workdir, "many.txt"), "w", newline="") as fh:
        fh.write("\n".join(str(i) for i in range(1, 21)) + "\n")
    out = _produce(["head", "many.txt"], workdir)
    assert out == "\n".join(str(i) for i in range(1, 11)) + "\n"


def test_head_dash_n_value(workdir):
    assert _produce(["head", "-n", "2", "a.txt"], workdir) == "one\ntwo\n"


def test_head_glued_short_count(workdir):
    assert _produce(["head", "-2", "a.txt"], workdir) == "one\ntwo\n"


def test_tail_dash_n_value(workdir):
    assert _produce(["tail", "-n", "1", "a.txt"], workdir) == "three\n"


def test_tail_glued_short_count(workdir):
    assert _produce(["tail", "-1", "a.txt"], workdir) == "three\n"


def test_tail_zero_count_is_empty(workdir):
    assert _produce(["tail", "-n", "0", "a.txt"], workdir) == ""


def test_head_tail_decline_multi_file(workdir):
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["head", "a.txt", "b.txt"])
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["tail", "a.txt", "b.txt"])


def test_head_declines_unsupported_flag(workdir):
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["head", "-c", "10", "a.txt"])


def test_head_declines_non_numeric_n(workdir):
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["head", "-n", "x", "a.txt"])


# --------------------------------------------------------------------------- sed


def test_sed_range(workdir):
    assert _produce(["sed", "-n", "2,3p", "a.txt"], workdir) == "two\nthree\n"


def test_sed_single_line(workdir):
    assert _produce(["sed", "-n", "2p", "a.txt"], workdir) == "two\n"


def test_sed_dollar_end_bound(workdir):
    assert _produce(["sed", "-n", "2,$p", "a.txt"], workdir) == "two\nthree\n"


def test_sed_dollar_single_line(workdir):
    assert _produce(["sed", "-n", "$p", "a.txt"], workdir) == "three\n"


def test_sed_without_dash_n_declines(workdir):
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["sed", "2,3p", "a.txt"])


def test_sed_other_program_declines(workdir):
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["sed", "-n", "s/a/b/", "a.txt"])


def test_sed_trailing_q_optimisation_declines(workdir):
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["sed", "-n", "20,40p;40q", "a.txt"])


def test_sed_out_of_bounds_range_declines(workdir):
    with pytest.raises(Unanswerable):
        _produce(["sed", "-n", "5,10p", "a.txt"], workdir)


# --------------------------------------------------------------------------- decline: operand set


def test_declines_redirection_gt(workdir):
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["cat", "a.txt", ">", "out.txt"])


def test_declines_redirection_append(workdir):
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["cat", "a.txt", ">>", "out.txt"])


def test_declines_redirection_stderr(workdir):
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["cat", "a.txt", "2>", "err.txt"])


def test_declines_command_substitution(workdir):
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["cat", "$(echo a.txt)"])


def test_declines_backtick_substitution(workdir):
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["cat", "`echo a.txt`"])


def test_declines_process_substitution(workdir):
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["cat", "<(echo a.txt)"])


def test_declines_variable_expansion_operand(workdir):
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["cat", "$HOME/a.txt"])


def test_sed_dollar_end_bound_survives_substitution_check(workdir):
    # `$` as sed's last-line bound is legitimate and must NOT be caught by the
    # same operand-position substitution check that rejects `$(...)`/`$VAR` in
    # a file operand -- it never reaches the operand position.
    assert _produce(["sed", "-n", "1,$p", "a.txt"], workdir) == "one\ntwo\nthree\n"


# --------------------------------------------------------------------------- decline: unsupported verb


def test_declines_unsupported_verb():
    with pytest.raises(Unanswerable):
        sr.parse_read_segment(["awk", "{print}", "a.txt"])


def test_declines_empty_segment():
    with pytest.raises(Unanswerable):
        sr.parse_read_segment([])


# --------------------------------------------------------------------------- decline: path resolution


def test_declines_glob_operand(workdir):
    with pytest.raises(Unanswerable):
        _produce(["cat", "*.txt"], workdir)


def test_declines_brace_operand(workdir):
    with pytest.raises(Unanswerable):
        _produce(["cat", "{a,b}.txt"], workdir)


def test_declines_nonexistent_path(workdir):
    with pytest.raises(Unanswerable):
        _produce(["cat", "does-not-exist.txt"], workdir)


def test_declines_directory(workdir):
    sub = os.path.join(workdir, "sub")
    os.mkdir(sub)
    with pytest.raises(Unanswerable):
        _produce(["cat", "sub"], workdir)


# --------------------------------------------------------------------------- decline: decoding


def test_declines_nul_byte_anywhere_in_file(workdir):
    path = os.path.join(workdir, "mostly-text.dat")
    # Text for well over the old 8192-byte NUL-scan window, binary after --
    # engine._read_text's window would miss this; this module must not.
    with open(path, "wb") as fh:
        fh.write(b"line\n" * 4000)
        fh.write(b"\x00")
    with pytest.raises(Unanswerable):
        _produce(["cat", "mostly-text.dat"], workdir)


def test_declines_non_utf8_bytes(workdir):
    path = os.path.join(workdir, "latin1.txt")
    with open(path, "wb") as fh:
        fh.write("café".encode("latin-1"))
    with pytest.raises(Unanswerable):
        _produce(["cat", "latin1.txt"], workdir)


# --------------------------------------------------------------------------- decline: read-size guard


def test_declines_oversized_file_without_reading(workdir):
    path = os.path.join(workdir, "big.txt")
    with open(path, "w") as fh:
        fh.write("x" * (MAX_RENDER_BYTES + 1))
    with pytest.raises(Unanswerable):
        _produce(["cat", "big.txt"], workdir)


def test_at_cap_file_is_still_served(workdir):
    path = os.path.join(workdir, "exact.txt")
    with open(path, "w") as fh:
        fh.write("x" * MAX_RENDER_BYTES)
    assert _produce(["cat", "exact.txt"], workdir) == "x" * MAX_RENDER_BYTES
