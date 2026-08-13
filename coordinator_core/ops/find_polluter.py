"""
coordinator_core.ops.find_polluter — ported from coordinator/bin/find-polluter.sh
(DOE-PORT R2-R6, variant #1 — pristine, no registered op, coordinator-claude keeps the .sh filename
as a polyglot trampoline over this module).

Purpose: bisection-style test-pollution finder. Given a filesystem path that should
NOT exist (`pollution_check`) and a glob test-pattern, runs `npm test <file>` once
per matched test file (in glob order) and reports the first test file whose run
causes `pollution_check` to spring into existence — the "polluter".

Usage (CLI, matches the bash oracle's argv contract exactly):
    find_polluter.main(["<file_or_dir_to_check>", "<test_pattern>"]) -> int

Resolution / behavior (unchanged from the bash oracle, `coordinator/bin/find-polluter.sh`):
  1. Pre-flight: `npm` must be resolvable on PATH — else fail loud (exit 1) before
     touching anything. Ported because the bash oracle's own header comment calls
     this out explicitly: "without it the test loop silently reports 'no polluter'".
  2. Exactly 2 positional args required — usage message + exit 1 otherwise.
  3. `test_pattern` is expanded as a Python `glob.glob(..., recursive=True)` — the
     Python-side equivalent of bash `globstar` (`**` matches across directory
     levels). Sorted for deterministic, reproducible bisection order (the bash
     oracle relies on `shopt -s globstar; TEST_FILES=($TEST_PATTERN)` filesystem
     enumeration order, which is not guaranteed sorted on every platform/shell —
     this port pins the order via an explicit sort, a faithful-intent tightening,
     not a behavior change on any tested corpus).
  4. Empty match set → fail loud (exit 1), remediation hint.
  5. Pre-flight: if `pollution_check` already exists BEFORE any test runs, fail
     loud (exit 1) — bisection can't attribute pre-existing pollution.
  6. For each matched test file, in order: run `npm test <file>`, discarding
     stdout/stderr (test failures are expected/ok — only pollution matters), then
     check whether `pollution_check` now exists. First hit reports the polluter
     (path, `ls -la`-style detail line) and returns 1. No hit across the full set
     returns 0 ("all tests clean").

Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Negative-spec (retired bash-only mechanics — NOT reintroduced here):
    - Does NOT use bash `set -euo pipefail` — Python's own exception propagation
      plus explicit `subprocess.run(..., check=False)` calls replace it; a
      `subprocess` failure never aborts the loop (mirrors the oracle's own
      `|| true` on the `npm test` call — non-zero test exit is expected/ok).
    - Does NOT shell out via `bash -c` for the glob expansion — `glob.glob` is the
      direct Python equivalent of bash globstar and needs no subprocess.
    - Faithfully REPRODUCES an oracle quirk, not a bug fix: `pollution_check` is
      checked with a bare existence test (`os.path.exists`, mirroring `[ -e ... ]`)
      — a directory, a symlink (dangling or not), a socket, all count as
      "polluted" exactly as the bash `[ -e ]` does. This is intentional fidelity,
      not an oversight.
    - Usage-message program name: the bash oracle interpolates `$0` (whatever
      path the caller invoked it by — absolute, relative, or bare-name-via-PATH).
      This port pins a literal `find-polluter.sh` instead of threading the
      trampoline's own argv[0] through — a deliberate simplification (the op
      module is invoked in-process, not as `$0`, so there is no single "correct"
      value to reproduce byte-for-byte across all invocation shapes anyway).
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from typing import List, Optional

_PROG = "find-polluter.sh"  # literal program-name prefix, matches the coordinator-claude filename


def _existence_detail(path: str) -> str:
    """Best-effort `ls -la <path>`-shaped detail line for the pollution report.

    Mirrors the oracle's `ls -la "$POLLUTION_CHECK"` call. Falls back to a bare
    existence note if `ls` is unavailable (never crashes the report path).
    """
    ls_bin = shutil.which("ls")
    if ls_bin is None:
        return f"{path} (exists)"
    try:
        result = subprocess.run(
            [ls_bin, "-la", path],
            capture_output=True,
            text=True,
            check=False,
            # Review: code-reviewer — Windows portability convention applied
            # inconsistently across this wave's siblings; align this call site.
            **no_console_creationflags(),
        )
        out = (result.stdout or "").rstrip("\n")
        return out if out else f"{path} (exists)"
    except OSError:
        return f"{path} (exists)"


def main(argv: Optional[List[str]] = None) -> int:
    # Line-buffer stdout even when redirected to a file/pipe — without this,
    # Python block-buffers stdout under non-tty redirection while stderr stays
    # unbuffered, interleaving differently than the bash oracle's line-at-a-time
    # `echo`. Content is identical either way; this keeps interleave order
    # identical too (matters for `2>&1 | tee`-style consumers).
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        print(f"skip: main: sys.stdout.reconfigure(line_buffering=True) failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass

    argv = list(sys.argv[1:] if argv is None else argv)

    if shutil.which("npm") is None:
        print(
            "Error: npm not found in PATH. Install Node.js/npm before running this script.",
            file=sys.stderr,
        )
        return 1

    if len(argv) != 2:
        print(f"Usage: {_PROG} <file_to_check> <test_pattern>")
        print(f"Example: {_PROG} '.git' 'src/**/*.test.ts'")
        return 1

    pollution_check, test_pattern = argv[0], argv[1]

    print(f"Searching for test that creates: {pollution_check}")
    print(f"Test pattern: {test_pattern}")
    print("")

    test_files = sorted(glob.glob(test_pattern, recursive=True))
    total = len(test_files)

    if total == 0:
        print(f"Error: no test files matched pattern '{test_pattern}'", file=sys.stderr)
        print(
            "Check that the pattern is correct and you are running from the right directory.",
            file=sys.stderr,
        )
        return 1

    print(f"Found {total} test files")
    print("")

    if os.path.exists(pollution_check):
        print(f"Error: {pollution_check} already exists before testing. Remove it first.")
        return 1

    for count, test_file in enumerate(test_files, start=1):
        print(f"[{count}/{total}] Testing: {test_file}")

        subprocess.run(
            ["npm", "test", test_file],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            # Review: code-reviewer — Windows portability convention applied
            # inconsistently across this wave's siblings; align this call site.
            **no_console_creationflags(),
        )

        if os.path.exists(pollution_check):
            print("")
            print("FOUND POLLUTER!")
            print(f"   Test: {test_file}")
            print(f"   Created: {pollution_check}")
            print("")
            print("Pollution details:")
            print(_existence_detail(pollution_check))
            print("")
            print("To investigate:")
            print(f"  npm test {test_file}    # Run just this test")
            print(f"  cat {test_file}         # Review test code")
            return 1

    print("")
    print("No polluter found - all tests clean!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
