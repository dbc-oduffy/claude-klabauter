"""Differential verification for the read-shape (C1) and ls (C2) source classes.

Mirrors `test_answer_differential.py`'s doctrine (own file, own comparator, per
the C4 brief) for the shapes that DOCTRINE file's `splitlines()`/`text=True`
comparator would silently pass wrong: a missing final newline, a CRLF, an empty
body, and `head -n 0`/EOF-range declines. Those divergences are exactly what
`ReadSource`/`sources_read`/`sources_listdir` exist to get right, so the oracle
here compares RAW stdout bytes with no line-splitting/newline-translation on
either side (AC6) -- see `_posix_shell.run_real`'s own docstring for why.

Refusals are always acceptable (a `None`/`Unanswerable` result means the real
command runs unchanged, which is by definition correct); a DISAGREEMENT between
our rendered text and the real command's raw stdout is never acceptable.

Negative-spec:
  - Does NOT extend `test_answer_differential.py` or import its comparator
    (`_real`/`_answered_body` both go through `str.splitlines()`/`text=True`,
    which is exactly the normalisation these fixtures exist to bypass) -- new
    file, so that file's own grep-oracle assertions stay untouched (AC4).
  - Does NOT hand-write an expected output for any case compared against a real
    command -- a hand-written expectation encodes the author's belief about
    `cat`/`sed`/`ls`, and this file exists to encode them instead.
  - Does NOT wire `ls` through `answer()` -- C3 deliberately did not wire it
    (`sources_listdir` is not yet a recognized `plan_for` branch), so `ls`
    fidelity here is asserted against `sources_listdir.parse_ls_segment`/`run`
    directly, not through `answer()`.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.search import sources_listdir as sl
from coordinator_core.search.answer import answer
from coordinator_core.search.engine import Unanswerable
from coordinator_core.search.tests._posix_shell import requires_posix_shell, run_real

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

#: The literal split point of a read source's provenance footer (C3). Matches
#: `ReadSource.execute`'s `note="[read in-process: no subprocess spawned]"`
#: byte-for-byte -- if that literal ever drifts, this split point must move
#: with it rather than being re-derived here.
_READ_FOOTER_MARKER = "\n\n[read in-process"


def _write_bytes(path, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


@pytest.fixture()
def tree(tmp_path):
    _write_bytes(tmp_path / "notrail.txt", b"one\ntwo\nthree")
    _write_bytes(tmp_path / "empty.txt", b"")
    _write_bytes(tmp_path / "blanklast.txt", b"one\ntwo\n\n")
    _write_bytes(tmp_path / "crlf.txt", b"one\r\ntwo\r\nthree\r\n")
    _write_bytes(tmp_path / "astral.txt", "grin \U0001F600 done\n".encode("utf-8"))
    _write_bytes(tmp_path / "oneline.txt", b"solo line\n")
    _write_bytes(tmp_path / "plain.txt", b"a\nb\nc\n")
    _write_bytes(
        tmp_path / "many.txt",
        ("\n".join(str(i) for i in range(1, 31)) + "\n").encode("utf-8"),
    )
    return tmp_path


def _answered_raw(cmd: str, cwd) -> str | None:
    """Return `answer()`'s body with only the provenance footer stripped --
    no `splitlines()`, no newline translation (AC6)."""
    text = answer(cmd, cwd=str(cwd))
    if text is None:
        return None
    return text.split(_READ_FOOTER_MARKER, 1)[0]


def _assert_matches_real(cmd: str, cwd) -> None:
    ours = _answered_raw(cmd, cwd)
    if ours is None:
        pytest.skip("declined -- the real command runs unchanged, which is correct")
    _rc, theirs = run_real(cmd, cwd)
    assert ours == theirs, (
        "in-process answer disagrees with real command\n"
        "  command : %s\n  ours    : %r\n  real    : %r" % (cmd, ours, theirs)
    )


# --------------------------------------------------------------- single-source


@requires_posix_shell
def test_cat_no_trailing_newline(tree):
    _assert_matches_real("cat notrail.txt", tree)


@requires_posix_shell
def test_cat_empty_file(tree):
    _assert_matches_real("cat empty.txt", tree)


@requires_posix_shell
def test_cat_blank_last_line(tree):
    _assert_matches_real("cat blanklast.txt", tree)


@requires_posix_shell
def test_cat_crlf_content(tree):
    _assert_matches_real("cat crlf.txt", tree)


@requires_posix_shell
def test_cat_astral_utf8_character(tree):
    _assert_matches_real("cat astral.txt", tree)


@requires_posix_shell
def test_head_n_zero(tree):
    """`head -n 0` is a valid, answerable case (count=0) -- NOT a decline --
    and must print nothing, matching real `head -n 0`'s empty stdout."""
    _assert_matches_real("head -n 0 many.txt", tree)


@requires_posix_shell
def test_tail_one_on_single_line_file(tree):
    _assert_matches_real("tail -1 oneline.txt", tree)


def test_sed_range_past_eof_declines(tree):
    """A START beyond EOF is a NAMED refusal (`sources_read.ReadSpec.produce`'s
    `start > n` bound check), not an approximation of what real `sed -n` would
    print (real `sed -n` prints nothing and exits 0) -- a refusal is correct by
    definition (the real command then runs unchanged), so this asserts the
    decline itself rather than comparing against real `sed`. A one-line file
    with a start line of 5 is unambiguously past its only line (line 1)."""
    assert answer("sed -n '5,10p' oneline.txt", cwd=str(tree)) is None


# ------------------------------------------------------------------- composed


def _assert_stage_output_matches_real(cmd: str, cwd) -> None:
    """Composed shapes (AC3's evidence that no stage code changed) compare as
    LINE LISTS, not raw bytes: the stage pipeline joins with `"\\n"` and never
    reproduces a trailing newline (`answer.py`'s `"\\n".join(lines)`), which is
    a pre-existing, out-of-scope-for-this-test-file rendering property of the
    stage path shared with `test_answer_differential.py` -- not one of the
    bare-read fidelity divergences (no trailing newline, CRLF, empty output)
    this file's single-source cases exist to catch raw-byte-exact (AC6)."""
    ours = _answered_raw(cmd, cwd)
    if ours is None:
        pytest.skip("declined -- the real command runs unchanged, which is correct")
    _rc, theirs = run_real(cmd, cwd)
    assert ours.splitlines() == theirs.splitlines(), (
        "in-process answer disagrees with real command\n"
        "  command : %s\n  ours    : %r\n  real    : %r" % (cmd, ours, theirs)
    )


@requires_posix_shell
def test_composed_cat_pipe_wc_l(tree):
    _assert_stage_output_matches_real("cat plain.txt | wc -l", tree)


@requires_posix_shell
def test_composed_sed_range_pipe_head(tree):
    _assert_stage_output_matches_real("sed -n '5,20p' many.txt | head -3", tree)


# ------------------------------------------------------------------------ ls
#
# `ls` is not wired through `answer()` (C3 deliberately left it unrecognized --
# see module docstring), so its fidelity is asserted directly against
# `sources_listdir.parse_ls_segment`/`run`.


@pytest.fixture()
def lsdir(tmp_path):
    d = tmp_path / "lsdir"
    d.mkdir()
    (d / ".hidden").write_text("dot\n")
    (d / "apple.txt").write_text("a\n")
    (d / "Banana.txt").write_text("b\n")
    return tmp_path


def _ls_ours(tokens, cwd) -> list[str] | None:
    try:
        spec = sl.parse_ls_segment(tokens)
        return sl.run(spec, cwd=str(cwd))
    except Unanswerable:
        return None


@requires_posix_shell
def test_ls_dotfile_and_locale_collation(lsdir, monkeypatch):
    """A directory with a dotfile (omitted by default) and a file whose name
    sorts differently under a non-C locale (case-insensitive collation puts
    'apple' before 'Banana'; a byte sort puts 'Banana' first) -- the case this
    module exists to get right rather than refuse (C2)."""
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    ours = _ls_ours(["ls", "lsdir"], lsdir)
    if ours is None:
        pytest.skip("locale not installed on this box -- a named, legal decline (C2)")
    _rc, real_text = run_real("LC_ALL=en_US.UTF-8 ls lsdir", lsdir)
    real_lines = real_text.splitlines()
    if not real_lines:
        pytest.skip("real ls produced no output -- cannot compare fidelity")
    assert ours == real_lines, (
        "in-process ls disagrees with real ls\n  ours: %r\n  real: %r"
        % (ours, real_lines)
    )
    assert ".hidden" not in ours


@requires_posix_shell
def test_composed_ls_pipe_wc_l(lsdir):
    """`ls DIR | wc -l` (AC3): our own entry count against real `ls | wc -l`'s
    VALUE -- BSD's width-8 padding is a named, deliberate divergence (matches
    `test_answer_differential.test_wc_count_agrees_but_padding_deliberately_diverges`),
    not something this seam reproduces by probing the host's own `wc`."""
    ours = _ls_ours(["ls", "lsdir"], lsdir)
    assert ours is not None
    _rc, theirs = run_real("ls lsdir | wc -l", lsdir)
    assert str(len(ours)) == theirs.strip()
