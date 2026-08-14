"""
coordinator_core.ops.verify_ps51_clean — PS 5.1-cleanliness smoke check for .ps1 files.

Purpose: repo-agnostic assertion that Windows setup/install .ps1 legs PARSE under
the Windows PowerShell 5.1 engine (powershell.exe, NOT pwsh 7) and contain no
pwsh-7-only syntax patterns. Four-way per-file classification:
  OK            — parses cleanly under PS 5.1 (optional WARN for advisory patterns)
  FAIL          — has PS 5.1 parse errors AND does NOT declare a PS7 requires floor
                  (accidental-incompatibility — a real defect)
  EXPECTED-PS7  — has PS 5.1 parse errors AND declares "#requires -Version 7.0"
                  (or higher); the 5.1 failure is by-design
  WARN          — advisory pwsh7-only syntax caught by a static scan; still OK,
                  does not trigger the fail-loud exit code

Windows-only check: on any platform where `powershell.exe` is not resolvable on
PATH, the whole run degrades to a SKIP (exit 0) — there is nothing to verify.

Exit codes (parity-critical — matches the bash oracle byte-for-byte):
  0 — all files parse-clean (WARN / EXPECTED-PS7 do not fail), OR the
      powershell.exe engine is absent (SKIP — Windows-only check)
  1 — one or more files have a genuine unexpected PS 5.1 parse error (FAIL)
  2 — usage error: called with no path/dir arguments

Port of: verify-ps51-clean.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: state/handoffs/2026-06-22_215618_windows-clean-install-verification.md
               (Part B)
               docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Negative-spec (faithful bash-oracle reproduction — do NOT "fix" these mid-port):
    - The static pwsh7-syntax scan is intentionally conservative: it flags `||`
      inside a string literal as an advisory WARN even though PS 5.1 parses that
      fine (the scan is bash-simple pattern matching, not a real PS tokenizer).
    - The static scan only runs for parse-clean files (parse_error_count == 0):
      EXPECTED-PS7 files intentionally use pwsh7 syntax (scan output would be
      noise) and FAIL files already have a primary verdict — this mirrors the
      bash oracle's deliberate skip, not an oversight.
    - `#requires -Version N` detection scans every line, not just line 1 — the
      bash oracle's `while read` loop has no "first line only" shortcut, so a
      `#requires` directive anywhere in the file (however unconventional) still
      floors the file. This is preserved, not corrected.
    - The PS7-floor detector only inspects the FIRST `#requires -Version` line
      it encounters (`break` on match) — a file with multiple conflicting
      `#requires` lines takes the first one, matching the bash oracle's `break`.

Port-shape note (path resolution): the bash oracle used `pwd -W` (Git-Bash/MSYS2/
Cygwin-only) to obtain a Windows-native absolute path before invoking
`powershell.exe`, because a bash *interpreter* on Windows needs that translation.
This Python port runs natively as a Windows process when powershell.exe is
resolvable (there is no POSIX-emulation layer to escape), so `os.path.abspath`
already yields the correct native (backslash) form — the `pwd -W` step has no
analogue here and is intentionally dropped, not a scope-drop regression.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from typing import List, Optional, Tuple

_PROG = "verify-ps51-clean.sh"  # literal program-name prefix, matches bash oracle's basename

# subprocess.run over the powershell.exe engine per file — bounded so one
# pathological .ps1 (or a wedged engine) cannot hang the whole scan (PORTER-BRIEF
# rule 2: every subprocess.run over external/looping input needs timeout + a
# stdin guard).
_PS_PARSE_TIMEOUT_SECS = 30

_REQUIRES_RE = re.compile(r"^[ \t]*#[Rr][Ee][Qq][Uu][Ii][Rr][Ee][Ss][ \t]")
_REQUIRES_VERSION_MAJOR_RE = re.compile(r"-[Vv]ersion[ \t]+([0-9]+)")
_REQUIRES_VERSION_FULL_RE = re.compile(r"-[Vv]ersion[ \t]+([0-9]+(?:\.[0-9]+)*)")

_PARSE_ERRORS_RE = re.compile(r"PARSE_ERRORS=([0-9]+)")
_FIRST_ERROR_RE = re.compile(r"FIRST_ERROR=(.+)")

_BOM = "﻿"


def _strip_bom(line: str) -> str:
    """Strip a leading UTF-8 BOM so #requires/scan regexes match BOM-encoded .ps1
    files (Windows mandates BOM on non-ASCII .ps1 for PS 5.1)."""
    if line.startswith(_BOM):
        return line[len(_BOM):]
    return line


def find_ps51_exe() -> Optional[str]:
    """Locate Windows PowerShell 5.1 via a PATH-only lookup (no console window)."""
    return shutil.which("powershell.exe")


def collect_ps1_files(args: List[str]) -> Tuple[List[str], List[str]]:
    """Collect .ps1 files from CLI args: files accepted directly (if .ps1),
    directories recursed (excluding node_modules/ and .git/ paths).

    Returns (files, warnings) — warnings are non-fatal "path not found" notices
    for the caller to print to stderr, mirroring the bash oracle's WARN-and-continue.
    """
    files: List[str] = []
    warnings: List[str] = []
    for arg in args:
        if os.path.isfile(arg):
            if arg.endswith(".ps1"):
                files.append(arg)
        elif os.path.isdir(arg):
            for root, dirs, names in os.walk(arg):
                dirs[:] = [d for d in dirs if d not in ("node_modules", ".git")]
                for name in sorted(names):
                    if name.endswith(".ps1"):
                        files.append(os.path.join(root, name))
        else:
            warnings.append(f"WARN: path not found, skipping: {arg}")
    return files, warnings


def detect_ps7_floor(text: str) -> Tuple[bool, Optional[str]]:
    """Scan every line for a `#requires -Version N` directive with major N >= 7.

    Returns (is_ps7_floored, requires_version_str). Only the PowerShell #requires
    directive counts, not arbitrary comments containing the word "requires".
    """
    for raw_line in text.splitlines():
        line = _strip_bom(raw_line)
        if not _REQUIRES_RE.match(line):
            continue
        major_match = _REQUIRES_VERSION_MAJOR_RE.search(line)
        if not major_match:
            continue
        try:
            major = int(major_match.group(1))
        except ValueError:
            print(f"skip: detect_ps7_floor: major = int(major_match.group(1)) failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
        if major >= 7:
            full_match = _REQUIRES_VERSION_FULL_RE.search(line)
            version_str = full_match.group(1) if full_match else major_match.group(1)
            return True, version_str
    return False, None


def static_scan_warnings(text: str) -> List[str]:
    """Advisory bash-simple scan for pwsh-7-only syntax patterns.

    Only meaningful for parse-clean files — see module negative-spec.
    """
    warn_tokens: List[str] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_bom(raw_line)
        if "??=" in line:
            warn_tokens.append(f"??= @line {lineno}")
        elif "??" in line:
            warn_tokens.append(f"?? @line {lineno}")
        if "?." in line or "?[" in line:
            warn_tokens.append(f"?./\\?[ @line {lineno}")
        if "&&" in line or "||" in line:
            warn_tokens.append(f"&&/|| @line {lineno}")
        if re.search(r"ForEach-Object[ \t]+-Parallel", line):
            warn_tokens.append(f"ForEach-Object -Parallel @line {lineno}")
    return warn_tokens


def _build_ps_command(win_path: str) -> str:
    # Escape single quotes for a PS single-quoted string ('' = literal ').
    escaped = win_path.replace("'", "''")
    # PS 5.1 on Windows accepts forward slashes fine; the bash oracle converted
    # to backslashes for parity with a Windows-native path form — preserved here.
    win_path_bs = escaped.replace("/", "\\")
    return (
        "$errs = $null; $null = "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{win_path_bs}', [ref]$null, [ref]$errs); "
        "$n = $errs.Count; Write-Output \"PARSE_ERRORS=$n\"; "
        "if ($n -gt 0) { Write-Output \"FIRST_ERROR=$($errs[0].Message) "
        "@line $($errs[0].Extent.StartLineNumber)\" }"
    )


def parse_with_ps51(ps51_exe: str, ps1_path: str) -> Tuple[int, str]:
    """Invoke the PS 5.1 AST parser on ps1_path (NO execution of the target script).

    Returns (parse_error_count, first_error_msg). A powershell.exe invocation
    failure (non-zero rc, or a timeout) is treated as a single parse failure,
    matching the bash oracle's `parse_rc -ne 0` fallback.
    """
    win_path = os.path.abspath(ps1_path)
    ps_command = _build_ps_command(win_path)
    encoded = base64.b64encode(ps_command.encode("utf-16-le")).decode("ascii")

    try:
        result = subprocess.run(
            [
                ps51_exe,
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            text=True,
            timeout=_PS_PARSE_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"skip: parse_with_ps51: result = subprocess.run( failed: {exc}", file=sys.stderr)
        return 1, f"powershell.exe invocation failed ({exc})"

    if result.returncode != 0:
        return 1, f"powershell.exe invocation failed (exit {result.returncode})"

    out = (result.stdout or "").replace("\r", "")
    parse_error_count = 0
    first_error_msg = ""
    m = _PARSE_ERRORS_RE.search(out)
    if m:
        parse_error_count = int(m.group(1))
    m2 = _FIRST_ERROR_RE.search(out)
    if m2:
        first_error_msg = m2.group(1)
    return parse_error_count, first_error_msg


def verdict_for_file(
    ps1: str,
    ps51_exe: str,
    parse_fn=None,
) -> str:
    """Compute and print the verdict line(s) for one file; return the tally bucket.

    Returns one of "OK" / "FAIL" / "EXPECTED-PS7" / "WARN" ("WARN" is a parse-clean
    file that also emitted advisory pwsh7-syntax lines — still counts as OK for the
    exit-code gate, tallied separately by the caller). `parse_fn` is swappable so
    tests can exercise the classification/tally logic without a real powershell.exe;
    default (None) resolves the module-level `parse_with_ps51` at CALL time (not
    def-time) so a `monkeypatch.setattr(module, "parse_with_ps51", ...)` in a test
    is honored even though this function is invoked without an explicit `parse_fn`.
    """
    if parse_fn is None:
        parse_fn = parse_with_ps51
    try:
        text = open(ps1, "r", encoding="utf-8", errors="replace").read()
    except OSError as exc:
        print(f"FAIL {ps1} — unreadable: {exc}")
        return "FAIL"

    is_ps7_floored, requires_version_str = detect_ps7_floor(text)
    parse_error_count, first_error_msg = parse_fn(ps51_exe, ps1)

    if parse_error_count > 0:
        if is_ps7_floored:
            print(
                f"EXPECTED-PS7 {ps1} — declares '#requires -Version "
                f"{requires_version_str}'; 5.1 parse errors are by-design"
            )
            return "EXPECTED-PS7"
        print(f"FAIL {ps1} — {parse_error_count} parse error(s): {first_error_msg}")
        return "FAIL"

    warn_tokens = static_scan_warnings(text)
    if warn_tokens:
        for tok in warn_tokens:
            print(f"WARN {ps1} — pwsh7-syntax: {tok}")
        return "WARN"
    print(f"OK {ps1}")
    return "OK"


def main(argv: List[str]) -> int:
    """CLI entry — mirrors verify-ps51-clean.sh Usage/Exit contract exactly.

    Usage: verify-ps51-clean.sh <path-or-dir> [<path-or-dir> ...]
    Exit:  0 if all files parse-clean under PS 5.1 (WARN/EXPECTED-PS7 are still 0),
           or if powershell.exe is absent (SKIP — Windows-only check);
           1 if any file has an unexpected 5.1 parse error (genuine FAIL);
           2 if called with no arguments (usage error);
           3 if the claude-klabauter-link import that reached this module failed transport-
             side — dedicated code, does NOT collide with 1 (business FAIL) or
             2 (CLI-usage error). Set by the DoE-side trampoline, not here (this
             module has no transport seam of its own — see trampoline comment).
    """
    if not argv:
        print(f"Usage: {_PROG} <path-or-dir> [<path-or-dir> ...]", file=sys.stderr)
        print(
            "  Each arg is a .ps1 file or a directory (recursed for *.ps1, "
            "excluding node_modules/ and .git/).",
            file=sys.stderr,
        )
        return 2

    ps51_exe = find_ps51_exe()
    if not ps51_exe:
        print(
            "SKIP: powershell.exe (Windows PowerShell 5.1) not present — "
            "PS5.1 cleanliness is a Windows-only check"
        )
        return 0

    ps1_files, path_warnings = collect_ps1_files(argv)
    for w in path_warnings:
        print(w, file=sys.stderr)

    total = len(ps1_files)
    if total == 0:
        print("No .ps1 files found in the specified paths.")
        print("0/0 parse-clean, 0 failed, 0 expected-ps7 (PS7-floored), 0 warned")
        return 0

    count_ok = 0
    count_fail = 0
    count_expected_ps7 = 0
    count_warn = 0

    for ps1 in ps1_files:
        verdict = verdict_for_file(ps1, ps51_exe)
        if verdict == "FAIL":
            count_fail += 1
        elif verdict == "EXPECTED-PS7":
            count_expected_ps7 += 1
        elif verdict == "WARN":
            count_warn += 1
            count_ok += 1
        else:  # "OK"
            count_ok += 1

    print("")
    print(
        f"{count_ok}/{total} parse-clean (incl. {count_warn} with advisory warnings), "
        f"{count_fail} failed, {count_expected_ps7} expected-ps7 (PS7-floored)"
    )

    return 1 if count_fail > 0 else 0
