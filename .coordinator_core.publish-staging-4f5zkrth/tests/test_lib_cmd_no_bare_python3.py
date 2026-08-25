"""Chunk C6 of docs/plans/2026-08-10-interpreter-surface-four-asks.md — bare-
`python3` regression guard for `coordinator/lib/**/*.cmd`.

A bare `python3` token in a `.cmd` file resolves through PATH at run time.
On Windows that can hit the WindowsApps stub or any other `python3` ahead of
the intended interpreter, so `.cmd` files under `coordinator/lib/` must name
a resolved interpreter instead. This guard already holds GREEN across the
tree today; it lands only the regression assertion, not a fix.

The invariant, exactly:

    Flag a word-boundary match on `python3` that is NOT immediately followed
    by `.` or a path separator (`/` or `\\`), scanned on non-comment lines
    only (a `.cmd` comment line is one whose first non-whitespace token is
    `REM` -- case-insensitive -- or `::`), across every file matching
    `coordinator/lib/**/*.cmd` recursively.

Consequences of that definition (each covered by
`test_matcher_positive_and_negative_cases` below):

- `python3.cmd` and `python3.exe` do NOT match (followed by `.`).
- A quoted path containing `.../python3/...` does NOT match (followed by a
  path separator).
- `python312` and `python3x` do NOT match (word-boundary fails -- `python3`
  is immediately followed by another word character).
- A bare `python3 foo.py` DOES match. So does `python3` at end-of-line, and
  `"python3"` (a quote is neither `.` nor a path separator).
- A `REM ... python3 ...` line and a `:: ... python3 ...` line do NOT match
  regardless of what follows `python3` -- comment lines are skipped
  entirely, before the followed-by check ever runs.

Known exempt fixture: `coordinator/lib/claude-home/claude-home.cmd` has a line
mentioning `templates/bin/python3.cmd` (line number not pinned here -- located
by content, since it has already moved once) that is exempt TWICE under this
definition -- it is a REM line, and the token is followed by `.`. No
path-based allowlist exists or is needed for it; the definition alone handles
it. Adding a per-file exemption to make this guard green would be treating a
matcher bug as a legitimate exception -- if a real violation ever needs
suppressing, fix the matcher's definition instead.

Scope: `coordinator/lib/**/*.cmd` only (not `coordinator/bin`, not
`templates/`) -- this guard's scope tracks its dispatch chunk exactly and
must not silently widen to surfaces this plan did not analyze.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB_ROOT = _REPO_ROOT / "coordinator" / "lib"

_WORD_BOUNDARY_PYTHON3 = re.compile(r"(?<![A-Za-z0-9_])python3(?![A-Za-z0-9_])")


def _is_comment_line(line: str) -> bool:
    """A `.cmd` comment line's first non-whitespace token is `REM`
    (case-insensitive) or `::`."""
    stripped = line.lstrip()
    if not stripped:
        return False
    upper = stripped.upper()
    if upper.startswith("REM") and (len(upper) == 3 or not upper[3].isalnum()):
        return True
    if stripped.startswith("::"):
        return True
    return False


def line_has_bare_python3(line: str) -> bool:
    """True iff `line` (a single, non-comment `.cmd` line) contains a
    word-boundary `python3` token NOT immediately followed by `.`, `/`, or
    `\\`. Comment-line filtering is the caller's responsibility (see
    `_is_comment_line`) -- this function judges one already-non-comment
    line's content only."""
    for match in _WORD_BOUNDARY_PYTHON3.finditer(line):
        end = match.end()
        following = line[end : end + 1]
        if following not in (".", "/", "\\"):
            return True
    return False


def find_bare_python3_violations(
    root: Path, paths: list[Path] | None = None
) -> list[tuple[str, int, str]]:
    """Recursively glob `root/**/*.cmd` (or use the caller-supplied `paths`
    if already computed), and return (relpath, lineno, line-text) for every
    non-comment line carrying a bare `python3` token per
    `line_has_bare_python3`."""
    violations: list[tuple[str, int, str]] = []
    for path in sorted(paths) if paths is not None else sorted(root.rglob("*.cmd")):
        try:
            relpath = path.resolve().relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            relpath = path.resolve().relative_to(root.resolve()).as_posix()
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _is_comment_line(line):
                continue
            if line_has_bare_python3(line):
                violations.append((relpath, lineno, line))
    return violations


def test_no_bare_python3_in_lib_cmd_files():
    """Standing regression guard: zero bare `python3` tokens across
    `coordinator/lib/**/*.cmd`. Must land GREEN -- the invariant already
    holds; this is the guard, not the fix."""
    paths = sorted(_LIB_ROOT.rglob("*.cmd"))
    assert len(paths) > 0, (
        f"Glob found zero .cmd files under {_LIB_ROOT} -- a broken scan "
        "root would silently pass forever and protect nothing."
    )

    violations = find_bare_python3_violations(_LIB_ROOT, paths=paths)
    assert violations == [], (
        "Found bare `python3` token(s) in coordinator/lib/**/*.cmd (must "
        "name a resolved interpreter, not rely on PATH resolution): "
        + "; ".join(f"{relpath}:{lineno}: {line!r}" for relpath, lineno, line in violations)
    )


def test_claude_home_cmd_python3_mention_stays_exempt():
    """Pins the one known python3 occurrence in the tree
    (`coordinator/lib/claude-home/claude-home.cmd`, located by content --
    the first line mentioning `python3` -- rather than a hardcoded line
    number, since an unrelated edit above it should not break this test)
    as exempt on both grounds the definition provides -- REM comment line,
    and the token is immediately followed by `.` -- with no path-based
    allowlist involved."""
    fixture_path = _LIB_ROOT / "claude-home" / "claude-home.cmd"
    lines = fixture_path.read_text(encoding="utf-8").splitlines()
    target_lineno = next(
        (lineno for lineno, line in enumerate(lines, start=1) if "python3" in line),
        None,
    )
    assert target_lineno is not None, (
        f"Expected a python3 mention somewhere in {fixture_path}; none found."
    )
    target_line = lines[target_lineno - 1]

    assert "python3" in target_line
    assert _is_comment_line(target_line), (
        f"Expected line {target_lineno} of {fixture_path} to be a REM/:: "
        f"comment line: {target_line!r}"
    )
    # Even judged on its content alone (ignoring the comment-line skip),
    # the token here is followed by "." -- exempt on the second ground too.
    assert not line_has_bare_python3(target_line)

    violations = find_bare_python3_violations(_LIB_ROOT)
    assert not any(
        relpath.endswith("claude-home/claude-home.cmd") and lineno == target_lineno
        for relpath, lineno, _ in violations
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("python3 foo.py", True),
        ("python3", True),
        ('"python3"', True),
        ("call python3 setup.py", True),
        ("python3.cmd", False),
        ("python3.exe", False),
        (r'"C:\tools\python3\python.exe"', False),  # abs-path-ok: fixture string, not a real path citation
        ("python3/bin/python", False),
        ("python312", False),
        ("python3x", False),
        ("REM python3 foo.py", False),
        ("  REM python3 foo.py", False),
        (":: python3 foo.py", False),
        ("rem python3 foo.py", False),
        ("foo :: python3", True),  # Review: coordinatorcode-reviewer-6f9e3ef7 -- mid-line `::` is not a comment marker
        ("foo REM python3", True),  # Review: coordinatorcode-reviewer-6f9e3ef7 -- mid-line `REM` is not a comment marker
        ("%python3%", True),  # Review: coordinatorcode-reviewer-6f9e3ef7 -- env-var expansion syntax, non-word chars both sides
    ],
)
def test_matcher_positive_and_negative_cases(line: str, expected: bool):
    """Unit-level proof of the predicate over every case the brief
    enumerates: bare python3, python3.cmd, python3.exe, a `.../python3/...`
    path, python312, a REM line, and a `::` line.

    Comment-line cases (`REM ...` / `:: ...`) are asserted through the full
    pipeline (`_is_comment_line` gating `line_has_bare_python3`) because
    `line_has_bare_python3` alone judges only line *content*, not whether
    the line is a comment -- that split mirrors `find_bare_python3_violations`.
    """
    if _is_comment_line(line):
        result = False
    else:
        result = line_has_bare_python3(line)
    assert result is expected, (line, result, expected)


def test_matcher_content_only_view_of_comment_lines_would_still_be_exempt_via_period():
    """Documents why the claude-home fixture line is doubly-exempt: even
    judged as bare content (bypassing the REM gate), the `.` immediately
    after `python3` in that specific line already exempts it via the
    followed-by rule alone."""
    line = "REM templates/bin/python3.cmd and coordinator/bin/coordinator-lesson-add.cmd."
    assert not line_has_bare_python3(line)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
