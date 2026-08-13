"""test_install_maximalist_cmd_clean_stderr.py — Windows smoke test for the
documented Windows entry point, `coordinator/scripts/install-maximalist.cmd`
(INSTALL.md §§ "Entry point" / "Windows note" — operators launching a cold
install from native cmd.exe/PowerShell are directed to this exact launcher).

WHY THIS EXISTS (2026-07-28 machine-a dogfood, finding F3): a stale-template
`.cmd` launcher (`setlocal enabledelayedexpansion` + parenthesized
`if (...) ... !ERRORLEVEL!` blocks — the pre-goto-refactor shape
`gen-launcher-shim.py` emitted before its argument-mangling fix, see that
module's `render_cmd` docstring "No `enabledelayedexpansion`" note) is a
known cmd.exe metacharacter-mangling hazard: delayed expansion makes cmd.exe
scan the WHOLE forwarded command line for `!...!` tokens before running it,
and a malformed/partial parse of that scan has been observed (same dogfood
session) to surface as garbage cmd.exe diagnostics --
"'iteral' is not recognized as an internal or external command",
"A subdirectory or file a already exists.", etc. -- burying the one
diagnostic line that actually mattered (an UntrustedRootError at line 41 of
~41 lines of noise). A silent .cmd regression here reintroduces exactly that
signal-budget failure for the next Windows operator.

This suite does NOT re-derive cmd.exe's parser; it black-box-invokes the
launcher exactly as INSTALL.md instructs an operator to, with a read-only
flag (`--help`, which every phase-orchestrator entrypoint in this family
supports and which performs zero mutation -- unlike `--check-only`, which
still probes live machine state and can legitimately emit real WARN/FATAL
diagnostics unrelated to this invariant), and asserts the output contains
none of the known mangling signatures and that the process exits 0.

Windows-only: skipped entirely on non-Windows platforms, matching the
sibling `.cmd`-launcher tests' convention of gating on os.name.

Spec backlink: coordinator-claude state/2026-07-28-machine-a-install-dogfood-friction-log.md § F3
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from coordinator_core.win_portability import no_console_creationflags

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_COORDINATOR_ROOT = os.path.dirname(os.path.dirname(_TESTS_DIR))  # bin/tests -> bin -> coordinator
_LAUNCHER = os.path.join(_COORDINATOR_ROOT, "scripts", "install-maximalist.cmd")

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="install-maximalist.cmd is a Windows-only launcher"
)

# Known cmd.exe metacharacter-mangling signatures (F3). Any of these appearing
# in the output of a clean `--help` invocation means prose (docstring text,
# usage text, ...) reached cmd.exe as commands instead of reaching Python as
# argv -- the exact defect this test guards against.
_MANGLING_SIGNATURES = (
    "is not recognized as an internal or external command",
    "was unexpected at this time",
    "Error occurred while processing:",
    "A subdirectory or file",  # md/mkdir collision text
)


# A `!`-bearing token is the ONLY argv shape that discriminates the fixed
# launcher from the pre-fix one. `--help` does not: under
# `setlocal enabledelayedexpansion` cmd.exe rewrites `fix!literal!bang` to
# `fixbang` before Python ever sees it, but leaves a bang-free `--help`
# byte-identical -- so a `--help`-only suite passes against the very template
# it exists to reject (2026-07-28 dogfood F11, the argv table under that
# finding). The token is deliberately unrecognized: `main()` validates argv
# ahead of every mutating step (F10) and echoes the rejected token verbatim to
# stderr before exiting 2, which makes the rejection path a read-only mirror of
# what cmd.exe actually forwarded.
_BANG_ARG = "--zz-nonexistent-flag=fix!literal!bang"
_MANGLED_BANG_ARG = "--zz-nonexistent-flag=fixbang"


def test_install_maximalist_cmd_help_is_clean() -> None:
    assert os.path.isfile(_LAUNCHER), f"launcher not found: {_LAUNCHER}"

    result = subprocess.run(
        ["cmd.exe", "/c", _LAUNCHER, "--help"],
        capture_output=True,
        text=True,
        check=False,
        **no_console_creationflags(),
    )
    combined = result.stdout + result.stderr

    assert result.returncode == 0, (
        f"install-maximalist.cmd --help exited {result.returncode}, expected 0\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    offenders = [sig for sig in _MANGLING_SIGNATURES if sig in combined]
    assert not offenders, (
        "install-maximalist.cmd emitted cmd.exe metacharacter-mangling "
        f"signature(s): {offenders} -- prose reached cmd.exe as commands "
        "(F3 regression). Full output:\n" + combined
    )


def test_install_maximalist_cmd_forwards_bang_bearing_argument_literally() -> None:
    """A literal `!` in a forwarded argument must survive the launcher intact.

    This is the assertion the sibling `--help` test cannot make. Against the
    pre-goto-refactor template (`setlocal enabledelayedexpansion`) the echoed
    token reads `--zz-nonexistent-flag=fixbang`; against the shipped template it
    reads back byte-for-byte. Anything that reintroduces delayed expansion to
    `gen-launcher-shim.py`'s `render_cmd` fails here.

    negative-spec: do NOT relax this to a substring check on `fix` or on
    `nonexistent-flag`. Both survive the mangling, so either one restores the
    vacuity this test was written to remove.
    """
    assert os.path.isfile(_LAUNCHER), f"launcher not found: {_LAUNCHER}"

    result = subprocess.run(
        ["cmd.exe", "/c", _LAUNCHER, _BANG_ARG],
        capture_output=True,
        text=True,
        check=False,
        **no_console_creationflags(),
    )
    combined = result.stdout + result.stderr

    assert result.returncode == 2, (
        f"expected the argv-validation exit 2 for {_BANG_ARG!r}, got "
        f"{result.returncode}. A different code means the token never reached "
        "install-maximalist.py's validate-first branch, so this test is not "
        f"measuring forwarding at all.\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    assert _MANGLED_BANG_ARG not in combined, (
        "the launcher mangled a literal `!` out of a forwarded argument: got "
        f"{_MANGLED_BANG_ARG!r}, expected {_BANG_ARG!r}. `enabledelayedexpansion` "
        "has been reintroduced to the .cmd template (2026-07-28 dogfood F3/F11)."
        "\nFull output:\n" + combined
    )
    assert _BANG_ARG in combined, (
        f"install-maximalist.py did not echo {_BANG_ARG!r} back verbatim. Either "
        "the launcher altered it in some way this test does not yet name, or the "
        "unrecognized-argument branch stopped mirroring the rejected token.\n"
        "Full output:\n" + combined
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
