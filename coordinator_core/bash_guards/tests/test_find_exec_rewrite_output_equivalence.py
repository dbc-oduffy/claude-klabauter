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

from coordinator_core.bash_guards.find_exec_rewrite import (
    _bt_find_exec_python_rewrite,
    _head_tail_count,
    _bt_parse_find_exec_segment,
)


def _build_fixture(root: Path) -> None:
    (root / "sub").mkdir(parents=True, exist_ok=True)
    (root / "a.txt").write_bytes(b"one\ntwo\n")
    (root / "sub" / "b.txt").write_bytes(b"three\nfour")
    (root / "sub" / "keep.md").write_bytes(b"not matched\n")


def _rewrite_body(command: str) -> str:
    parsed = _bt_parse_find_exec_segment(shlex.split(command))
    assert parsed is not None, "fixture command must parse as a find -exec segment"
    rewrite = _bt_find_exec_python_rewrite(parsed)
    assert rewrite is not None, "fixture command must be translatable"
    return shlex.split(rewrite)[-1]


def _run_rewrite(command: str, cwd: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.chdir(cwd)
    buf = io.StringIO()
    with redirect_stdout(buf):
        exec(compile(_rewrite_body(command), "<rewrite>", "exec"), {"__name__": "__main__"})
    return buf.getvalue()


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


_HT_FILES = {
    "a.txt": b"1\n2\n3\n4\n",
    "b.txt": b"x\ny\nz",
    "c.txt": b"",
}
_HT_CASES = [
    "head -1", "head -n 3", "head", "head -n 0", "tail -2", "tail -n 2", "tail", "tail -n 0",
]


def _ht_fixture(root: Path) -> None:
    for name, data in _HT_FILES.items():
        (root / name).write_bytes(data)


def test_head_tail_goldens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ht_fixture(tmp_path)
    run = lambda c: _run_rewrite("find . -name '*.txt' -exec %s {} \\;" % c, tmp_path, monkeypatch)
    out = run("head -1")
    assert sorted(out.splitlines(keepends=True)) == ["1\n", "x\n"]
    assert "==>" not in out
    assert run("tail -2") == "3\n4\ny\nz\n"
    assert run("head -n 0") == ""


@pytest.mark.parametrize(
    "exec_argv",
    [
        ["head", "-c", "5", "{}"],
        ["tail", "-f", "{}"],
        ["tail", "-n", "+3", "{}"],
        ["head", "-n3", "{}"],
        ["head", "-n", "²", "{}"],
        ["head", "-²", "{}"],
        ["head", "-n", "3", "-q", "{}"],
        ["head", "-n", "3"],
        ["head"],
    ],
)
def test_head_tail_unsupported_shapes_are_not_translated(exec_argv: List[str]) -> None:
    assert _head_tail_count(exec_argv) is None
    parsed = {"path": ".", "name_pattern": None, "only_files": True,
              "exec_argv": exec_argv, "terminator": "semi"}
    assert _bt_find_exec_python_rewrite(parsed) is None


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("verb_args", _HT_CASES)
def test_head_tail_rewrite_matches_real_semi_form(
    verb_args: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    find, verb = shutil.which("find"), shutil.which(verb_args.split()[0])
    if not (find and verb):
        pytest.skip("find/head/tail not on PATH")
    probe = subprocess.run([find, "--version"], capture_output=True, text=True, **no_console_creationflags())
    if probe.returncode != 0 or "find" not in probe.stdout.lower():
        pytest.skip("PATH find is not POSIX find")
    _ht_fixture(tmp_path)
    cmd = "find . -type f -exec %s {} \\;" % verb_args
    proc = subprocess.run(
        [find, ".", "-type", "f", "-exec", verb_args.split()[0], *verb_args.split()[1:], "{}", ";"],
        cwd=tmp_path, capture_output=True, **no_console_creationflags(),
    )
    if proc.returncode != 0:
        pytest.skip("PATH find could not run the ';' form: %r" % proc.stderr[:200])
    real = proc.stdout.decode("utf-8", "replace")
    assert _run_rewrite(cmd, tmp_path, monkeypatch) == real


def test_cat_rewrite_appends_nothing_to_a_file_without_a_trailing_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "only.txt").write_bytes(b"no trailing newline")
    out = _run_rewrite("find . -name '*.txt' -exec cat {} \\;", tmp_path, monkeypatch)
    assert out == "no trailing newline"


def test_wc_rewrite_counts_newlines_not_iterated_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "only.txt").write_bytes(b"a b c")
    out = _run_rewrite("find . -name '*.txt' -exec wc -l {} \\;", tmp_path, monkeypatch)
    assert out == "0 ./only.txt\n"


def test_rm_rewrite_still_removes_exactly_the_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_fixture(tmp_path)
    _run_rewrite("find . -name '*.txt' -exec rm {} \\;", tmp_path, monkeypatch)
    survivors = sorted(
        str(p.relative_to(tmp_path)).replace(os.sep, "/")
        for p in tmp_path.rglob("*")
        if p.is_file()
    )
    assert survivors == ["sub/keep.md"]
