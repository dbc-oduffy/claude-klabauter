"""coordinator/bin/tests/test_cmd_rem_line_metacharacters.py — the gate that
keeps redirection/pipe/background metacharacters and cross-line-unbalanced
double quotes out of `.cmd` REM comment prose.

WHY THIS EXISTS. cmd.exe parses `<`, `>`, `|`, and `&` as redirection/pipe/
background operators, and tracks double-quote balance, INSIDE `REM` lines too
-- a `REM` line is not immune at the tokenizer level. A prose placeholder like
`<settings-home>`, a version comparison written with a literal `>=`, or a
quoted UI string that spans two `REM` lines (opens a `"` on one line, closes
it on the next) silently corrupts the parse of the REST of the script: cmd
treats the metacharacter as real redirection/piping, or keeps consuming
subsequent lines as part of one unterminated quoted token, and everything
downstream is misparsed into a spray of "'x' is not recognized as an
internal or external command" / "was unexpected at this time" errors.

This is not hypothetical -- on 2026-07-28 this exact class of defect broke
`<settings-home>/bin/machine-local.cmd` (a `>=` version comparison against
3.11 in a REM line) -- the canonical registry CLI every coordinator install depends
on -- corrupting it into spraying garbage instead of returning a value,
which in turn made every caller read empty output as "registry key unset"
and blocked percolation entirely on that machine. That launcher's template
lives in example-doctrine-repo (`templates/bin/machine-local.cmd`), not here; this port
exists because claude-klabauter independently tracks 353 generated `.cmd` launchers of
its own (see `coordinator/bin/gen-launcher-shim.py`) with no equivalent gate
prior to this file. See example-doctrine-repo's `coordinator/docs/wiki/windows-cmd-
shims.md` for the full incident writeup.

WHY A WHOLE-LINE SCAN, NOT A SUBSTRING DENYLIST. Any bare `<`, `>`, `|`, or
`&` on a REM line is a landmine regardless of surrounding prose -- there is
no "safe" placement, so the check is unconditional. Quote balance is checked
per-line (an odd count of `"` on one REM line means a quote opened there
was not closed on the same line) rather than accumulated across the file,
because a REM block deliberately closing every quote it opens, even when a
DIFFERENT REM line elsewhere also opens and closes its own pair, must not
false-positive.

SCOPE. Every tracked `.cmd` in the repo, enumerated via `git ls-files` so a
new launcher anywhere in the tree is covered the moment it is committed.

Ported from example-doctrine-repo `coordinator/tests/test_cmd_rem_line_metacharacters.py`
(2026-07-28); the checker function and both fixtures are preserved verbatim.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags

REPO_ROOT = Path(__file__).resolve().parents[3]

_REM_LINE_RE = re.compile(r"^\s*REM\b(.*)$", re.IGNORECASE)

# Bare redirection/pipe/background characters. Deliberately unconditional --
# no "except when balanced" carve-out, since `<file>`-style prose placeholders
# are exactly the shape that broke in production (a bare `<`/`>` is a
# redirection operator to cmd.exe regardless of whether a human reader would
# parse it as a matched pair).
_METACHAR_RE = re.compile(r"[<>|&]")


def find_rem_metachar_violations(content: str) -> list[tuple[int, str]]:
    """Return (1-indexed line number, line text) for every REM line in
    `content` that either contains a bare `<`, `>`, `|`, or `&`, or leaves a
    double quote unclosed on that same line (an odd `"` count) -- the two
    confirmed-reproducible triggers behind the 2026-07-28 machine-local.cmd
    production break. Empty list means clean.
    """
    violations: list[tuple[int, str]] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        m = _REM_LINE_RE.match(line)
        if not m:
            continue
        rest = m.group(1)
        if _METACHAR_RE.search(rest):
            violations.append((lineno, line))
        elif rest.count('"') % 2 == 1:
            violations.append((lineno, line))
    return violations


@lru_cache(maxsize=1)
def _tracked_cmd_files() -> tuple[str, ...]:
    """Every tracked `.cmd` path, repo-relative. Cached -- one `git ls-files`
    spawn per module, not one per assertion."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.cmd"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        **no_console_creationflags(),
        check=True,
    ).stdout
    return tuple(p for p in out.split("\0") if p)


def test_checker_flags_a_deliberately_broken_fixture():
    """Proves the checker itself is not vacuous: a REM block that reproduces
    the exact 2026-07-28 machine-local.cmd shapes (a bare `>=` version
    comparison, and a double-quoted phrase left open across two REM lines)
    must be caught. If this test ever fails, the checker has regressed to a
    no-op and the real-file scan below is not trustworthy."""
    broken = (
        "@echo off\n"
        "REM probes multiple python3.NN candidates for a\n"
        "REM >=3.11 match before falling back to first-found\n"
        "REM falls through to ShellExecute, which pops the \"Select an app to open\n"
        'REM \'foo\'" Open-With picker.\n'
        "exit /b 0\n"
    )
    violations = find_rem_metachar_violations(broken)
    flagged_lines = {lineno for lineno, _ in violations}
    assert 3 in flagged_lines, "did not catch the bare `>=` redirection landmine"
    assert 4 in flagged_lines, "did not catch the cross-line-unbalanced quote landmine"


def test_checker_passes_a_clean_fixture():
    """A REM block that avoids both landmines (spells out the comparison and
    keeps quotes balanced per line) must not be flagged -- guards against an
    overzealous checker that would make every launcher unwritable."""
    clean = (
        "@echo off\n"
        "REM probes multiple python3.NN candidates for a 3.11-or-newer match\n"
        'REM falls through to ShellExecute, which pops the Open-With picker.\n'
        "exit /b 0\n"
    )
    assert find_rem_metachar_violations(clean) == []


def test_no_cmd_has_rem_line_metacharacters():
    """Every tracked `.cmd`'s REM prose must be free of bare `<`/`>`/`|`/`&`
    and cross-line-unbalanced double quotes. Asserted on the emitted shape of
    every tracked launcher, not on a comment claiming the invariant holds."""
    cmd_files = _tracked_cmd_files()
    assert cmd_files, "no tracked .cmd files found -- gate is vacuous"

    offenders: list[str] = []
    for rel in cmd_files:
        content = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        violations = find_rem_metachar_violations(content)
        if violations:
            lines = ", ".join(f"L{lineno}" for lineno, _ in violations)
            offenders.append(f"{rel} ({lines})")

    assert not offenders, (
        "REM line(s) with a bare `<`/`>`/`|`/`&` or a cross-line-unbalanced "
        "double quote in:\n  "
        + "\n  ".join(offenders)
        + "\ncmd.exe parses redirection/pipe/background metacharacters and "
        "quote balance INSIDE REM lines too -- this exact shape corrupted "
        "example-doctrine-repo's templates/bin/machine-local.cmd in production on "
        "2026-07-28. Rephrase the prose to avoid literal angle brackets, "
        "pipes, ampersands, and multi-line quoted phrases; do not "
        "hand-suppress this check."
    )
