#!/usr/bin/env python3
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

Spec backlink: example-doctrine-repo state/2026-07-28-machine-a-install-dogfood-friction-log.md § F3
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_COORDINATOR_ROOT = os.path.dirname(os.path.dirname(_TESTS_DIR))  # bin/tests -> bin -> coordinator
_LAUNCHER = os.path.join(_COORDINATOR_ROOT, "scripts", "install-maximalist.cmd")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

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


def test_install_maximalist_cmd_help_is_clean() -> None:
    assert os.path.isfile(_LAUNCHER), f"launcher not found: {_LAUNCHER}"

    result = subprocess.run(
        ["cmd.exe", "/c", _LAUNCHER, "--help"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=_NO_WINDOW,
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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
