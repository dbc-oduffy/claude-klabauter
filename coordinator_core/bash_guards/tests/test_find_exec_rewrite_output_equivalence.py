"""The `find -exec` rewrite must produce the output it replaces.

Purpose: `check_find_exec_rewrite` does not merely advise -- it hands the
operator a `python3 -c` one-liner to run INSTEAD of their command. A rewrite
whose output differs from the command it replaces is worse than no rewrite:
the operator follows the guard, gets a plausible-looking answer, and has no
signal that it is a different answer.

All three shipped translations differed. Measured 2026-08-31 against real
`find` on a two-file fixture, one file deliberately without a trailing
newline:

    cat    real 'one\\ntwo\\nthree\\nfour'   was 'one\\ntwo\\n\\nthree\\nfour\\n'
    wc -l  real '2 ./a.txt\\n1 ./sub/b.txt\\n'  was '4\\n'
    rm     real ''                          was '2 file(s) removed\\n'

`cat` gained one newline per matched file (and silently terminated a file
that had none). `wc -l` collapsed a per-file breakdown into a grand total,
and the total was itself wrong -- `wc -l` counts newline CHARACTERS, while
iterating a file object yields a final unterminated line as a line, so the
fixture's 3 became 4. `rm` printed a progress line where `find` prints
nothing.

The goldens below are those measured real-`find` outputs, transcribed. This
file does NOT shell out to `find` to re-derive them: `find`/`wc` are not
present on every host this suite runs on, and the amplification gate
(`test_no_unbatched_per_item_git_spawn.py`) is the standing reason not to
add per-case process spawns to a guard suite. The rewrite body is executed
IN-PROCESS instead, which is what makes each case a few milliseconds rather
than a few hundred.

Negative-spec: this file asserts on the rewrite's OUTPUT and filesystem
effect, never on whether `check_find_exec_rewrite` fires, which verb set is
translatable, or what the advisory says. Two cosmetic properties are
deliberately NOT asserted, because the rewrite deliberately does not
reproduce them -- `wc`'s column padding (GNU and BSD/msys builds disagree,
so there is no single correct spelling) and the native path separator (this
normalizes to `/`, since the POSIX command being replaced emits `/` on
every host).
"""

from __future__ import annotations

import io
import os
import shlex
from contextlib import redirect_stdout
from pathlib import Path
from typing import List, Tuple

import pytest

from coordinator_core.bash_guards.dispatch_checks import (
    _bt_find_exec_python_rewrite,
    _bt_parse_find_exec_segment,
)


def _build_fixture(root: Path) -> None:
    """Two matching files and one non-matching, across two directory levels.

    `sub/b.txt` has NO trailing newline -- the discriminator between counting
    newline characters (`wc -l`: 1) and counting iterated lines (2), and the
    file `cat` was silently terminating."""
    (root / "sub").mkdir(parents=True, exist_ok=True)
    (root / "a.txt").write_bytes(b"one\ntwo\n")
    (root / "sub" / "b.txt").write_bytes(b"three\nfour")
    (root / "sub" / "keep.md").write_bytes(b"not matched\n")


def _rewrite_body(command: str) -> str:
    """The python source `check_find_exec_rewrite` would hand the operator."""
    parsed = _bt_parse_find_exec_segment(shlex.split(command))
    assert parsed is not None, "fixture command must parse as a find -exec segment"
    rewrite = _bt_find_exec_python_rewrite(parsed)
    assert rewrite is not None, "fixture command must be translatable"
    # `<python3> -c <quoted body>` -- the body is the final token.
    return shlex.split(rewrite)[-1]


def _run_rewrite(command: str, cwd: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.chdir(cwd)
    buf = io.StringIO()
    with redirect_stdout(buf):
        exec(compile(_rewrite_body(command), "<rewrite>", "exec"), {"__name__": "__main__"})
    return buf.getvalue()


#: `(label, command, golden)` -- golden is the measured real-`find` stdout.
_OUTPUT_CASES: List[Tuple[str, str, str]] = [
    ("cat", "find . -name '*.txt' -exec cat {} \\;", "one\ntwo\nthree\nfour"),
    ("wc-l", "find . -name '*.txt' -exec wc -l {} \\;", "2 ./a.txt\n1 ./sub/b.txt\n"),
    ("rm", "find . -name '*.txt' -exec rm {} \\;", ""),
]


@pytest.mark.parametrize(
    "command,golden",
    [pytest.param(c, g, id=label) for label, c, g in _OUTPUT_CASES],
)
def test_rewrite_output_matches_real_find(
    command: str, golden: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_fixture(tmp_path)
    assert _run_rewrite(command, tmp_path, monkeypatch) == golden


def test_cat_rewrite_appends_nothing_to_a_file_without_a_trailing_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single-file case, isolated. With one match there is no
    concatenation to get wrong, so a per-file trailing newline is the ONLY
    thing that can differ -- and it is the defect that survives a
    multi-file test written carelessly enough to strip whitespace."""
    (tmp_path / "only.txt").write_bytes(b"no trailing newline")
    out = _run_rewrite("find . -name '*.txt' -exec cat {} \\;", tmp_path, monkeypatch)
    assert out == "no trailing newline"


def test_wc_rewrite_counts_newlines_not_iterated_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`wc -l` counts newline characters. A file of three unterminated
    words on one line is 0, not 1."""
    (tmp_path / "only.txt").write_bytes(b"a b c")
    out = _run_rewrite("find . -name '*.txt' -exec wc -l {} \\;", tmp_path, monkeypatch)
    assert out == "0 ./only.txt\n"


def test_rm_rewrite_still_removes_exactly_the_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silencing the progress line must not silence the work. The effect is
    the half of `rm` that was always correct, and dropping a `print` is
    exactly the edit that could take the loop body with it."""
    _build_fixture(tmp_path)
    _run_rewrite("find . -name '*.txt' -exec rm {} \\;", tmp_path, monkeypatch)
    survivors = sorted(
        str(p.relative_to(tmp_path)).replace(os.sep, "/")
        for p in tmp_path.rglob("*")
        if p.is_file()
    )
    assert survivors == ["sub/keep.md"]
