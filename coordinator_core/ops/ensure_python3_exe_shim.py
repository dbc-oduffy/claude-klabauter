"""
coordinator_core.ops.ensure_python3_exe_shim — installs a python3.exe alongside
python.exe on Windows.

Why this exists: on Windows, Win32 CreateProcess does NOT honor PATHEXT and
only finds .exe files. So any child process re-resolution keyed off the bare
name "python3" (list-form process spawn, no shell) fails CreateProcess ->
falls through to ShellExecute -> Windows pops the "Select an app to open
'python3'" file-association dialog. The python.org installer ships
python.exe/pythonw.exe but NOT python3.exe. lib/resolve-python.sh (bash) and
the python3.cmd shim in templates/bin/ both fix the bash-side and cmd.exe-side
resolution paths, but neither helps CreateProcess. A real .exe is the only
thing that satisfies CreateProcess's lookup.

What this does (Windows only; no-op elsewhere):
    1. Resolve the target interpreter via coordinator_core.pyresolve
       (in-process — no subprocess CLI hop, unlike the bash oracle, which had
       to shell back into this same module because it was itself bash; see
       "Direct-import adaptation" below).
    2. If the python3 shim exists AND matches the python binary (hardlink:
       implicit; copy: byte-equal), no-op. If divergent, re-shim.
    3. Try a hardlink (os.link — Win32 CreateHardLinkW under the hood; same
       inode, no file-data duplication; auto-tracks the source binary's
       updates because they share inode). No elevation required on NTFS.
    4. Fall back to a plain copy if the hardlink fails (e.g. cross-volume,
       non-NTFS).

Idempotent: safe to run repeatedly. Re-running on an existing valid shim is a
no-op; on a stale copy-fallback shim (source binary patched but copy not),
re-shims.

WSL note: sys.platform in WSL reports the Linux platform value (WSL processes
use Linux namespacing; CreateProcess is not involved), so the OS gate
correctly no-ops.

Direct-import adaptation: the bash oracle (coordinator/bin/install-health/
ensure-python3-exe-shim.sh, DoE-claude) bootstrapped a bare off-PATH
interpreter, then shelled BACK into coordinator_core.pyresolve's print-bin
CLI mode (a subprocess hop) because the caller was itself bash. This module
IS that claude-klabauter process now, so it calls
coordinator_core.pyresolve.resolve_python_bin() directly in-process — the
same simplification the auto_push/handoff_gate_aging ports made (see their
module docstrings). PythonPinInvalid (a bad pin found on disk) is mapped to
the running interpreter's own executable path — the analogue of the bash
oracle's own bootstrap interpreter (the bare off-PATH lookup), i.e. "the
interpreter this process is already running under" in both shapes.

Port source: coordinator/bin/install-health/ensure-python3-exe-shim.sh
(DoE-claude). The DoE .sh keeps its filename AND its bash-side security guard
(coordinator-trusted-root-guard.sh, a bash-only sourced lib out of this
port's scope) — it is now a thin veneer: bash runs the trust guard, then
invokes this module's main() in a child interpreter, rather than the
sh/python polyglot-exec trampoline shape used elsewhere. The polyglot shape
hands the WHOLE file to python on hand-off (python re-parses the file from
line 1), so bash-only statements between the shebang and the exec line would
break under python — incompatible with keeping the trust guard live. See the
.sh's own header comment for the mechanical detail.

Spec backlink: DoE-claude:pln-bash-polyglot-clean-slate-full-5c71ee

Negative-spec:
    - Never mutates when CHECK_ONLY is set — reports what would happen and
      returns, even when a stale shim was detected (matches the bash oracle:
      the CHECK_ONLY gate sits AFTER the stale-detection branch, before the
      actual removal+re-shim).
    - Does not attempt to repair an AppX zero-byte reparse-point stub at the
      shim path — reports the pre-existing hazard and exits 1 (fail-loud),
      directing the operator to install-substrate.sh's cleanup prompt.
      Reproduced verbatim from the bash oracle; not "fixed" in this port.

---

"install.detect_python3_appx_stub" op (registered below, EXTEND per
state/audits/2026-07-22-command-payload-inventory/op-classification.tsv row
"detect-python3-appx-stub"): resolves "python3" on PATH and three-way
classifies it — "not_found" (nothing resolves), "stub" (the Windows Store
App-Execution-Alias 0-byte reparse-point python3 stub — Windows enumerates
it as a real PATH hit, but launching it does not run Python, it pops the
Store), or "ready" (a real interpreter that answers `--version`). Reuses
`_is_appx_stub()` (the same `.exists() and not .is_file()` signature
`_install_shim()` already checks at the shim path above) rather than
re-deriving the detection — the input signal is identical, only the
resolved path differs (PATH-resolved "python3" here vs. the shim-directory
"python3.exe" there). Safe (clean "not_found"/"ready" result, never a false
"stub") on macOS/Linux: the AppX reparse-point stub is a Windows-only
on-disk shape, so `_is_appx_stub()` is vacuously false off Windows, and
`shutil.which("python3")` / a real `--version` subprocess behave ordinarily
there.

DEC-7 idempotency note (platform-hazard rated `high` per the manifest row,
per CLAUDE.md § Runtime conventions / plan DEC-7): this op is a pure
read-only probe — it resolves PATH and (at most) spawns one `--version`
subprocess; it never writes, moves, links, or deletes anything. Two
back-to-back invocations against identical PATH/filesystem state always
observe the same interpreter and re-run the same read-only checks, so they
return the same classification — idempotent by construction (no state
transition to converge, not merely "happens not to fail twice"). The `high`
platform rating on this row describes the classification's *subject*
(a Windows-only failure mode), not an idempotency risk — there is no
"already-classified" state this op could stomp on re-invocation, unlike
`_install_shim()`'s file-mutating path above.

Spec backlink: pln-coordinator-ops-buildout-from--903224
"""

from __future__ import annotations

import asyncio
import filecmp
import ntpath
import os
import shutil
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.ipc import register_op

_PYTHON_EXE_NAME = "python" + ".exe"
_PYTHON3_EXE_NAME = "python3" + ".exe"
_LAUNCHER_NAMES = frozenset({"py", "pyw"})
_LOG_PREFIX = "[ensure-python3-exe-shim]"

_APPX_STUB_PROBE_TIMEOUT_S = 10


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _resolve_python_bin() -> str:
    from coordinator_core.pyresolve import (
        PythonPinInvalid,
        _WINDOWLESS_BASENAMES,
        _console_sibling,
        resolve_python_bin,
    )

    def _reject_windowless(candidate: str) -> str:
        if not candidate:
            return candidate
        if ntpath.basename(candidate).lower() not in _WINDOWLESS_BASENAMES:
            return candidate
        sibling = _console_sibling(candidate)
        if sibling:
            return sibling
        print(
            f"{_LOG_PREFIX} WARNING: resolved interpreter '{candidate}' is windowless "
            "with no console sibling on disk; no interpreter to shim",
            file=sys.stderr,
        )
        return ""

    try:
        python_bin, _args = resolve_python_bin(prefer_windowless=False)
    except PythonPinInvalid:
        return _reject_windowless(sys.executable or "")
    return _reject_windowless(python_bin or "")


def _is_appx_stub(path: Path) -> bool:
    return path.exists() and not path.is_file()


def _exe_basename_pth_trap(py_exe: Path, py3_exe: Path) -> Optional[Path]:
    candidate = py_exe.parent / f"{py_exe.stem}._pth"
    if candidate.is_file():
        return candidate
    return None


def _install_shim(python_bin: str, check_only: bool) -> int:
    """Core shim logic, ported 1:1 from the bash oracle's post-PYTHON_BIN-
    resolution body. Returns the process exit code."""
    if not python_bin:
        print(
            f"{_LOG_PREFIX} no Python interpreter resolved (PYTHON_BIN unset); skipping",
            file=sys.stderr,
        )
        return 0

    if python_bin in _LAUNCHER_NAMES:
        print(
            f"{_LOG_PREFIX} resolver returned launcher ({python_bin}); skipping "
            "(need a real install dir to shim)",
            file=sys.stderr,
        )
        return 0

    py_dir = Path(python_bin).resolve().parent if os.path.isabs(python_bin) else Path(python_bin).parent
    py_exe = py_dir / _PYTHON_EXE_NAME
    py3_exe = py_dir / _PYTHON3_EXE_NAME

    if not py_exe.is_file():
        print(
            f"{_LOG_PREFIX} {_PYTHON_EXE_NAME} not found at {py_exe} (PYTHON_BIN={python_bin}); skipping",
            file=sys.stderr,
        )
        return 0

    pth_trap = _exe_basename_pth_trap(py_exe, py3_exe)
    if pth_trap is not None:
        print(
            f"{_LOG_PREFIX} ERROR: {pth_trap} pins isolation to {py_exe.name}'s own "
            "basename ('._pth' files are looked up by executable basename, not DLL "
            f"name); shimming {py3_exe.name} here would look for a nonexistent "
            f"{py3_exe.stem}._pth and silently fall back to full registry+env+site "
            "path resolution -- isolated mode would be off with no error. Refusing "
            "to shim.",
            file=sys.stderr,
        )
        return 1

    if _is_appx_stub(py3_exe):
        print(
            f"{_LOG_PREFIX} {py3_exe} exists but is not a regular file "
            "(likely AppX zero-byte reparse stub)",
            file=sys.stderr,
        )
        print(
            f"{_LOG_PREFIX}   Run install-substrate.sh and accept the orphan-stub "
            "deletion prompt, then re-run this script.",
            file=sys.stderr,
        )
        return 1

    stale = False
    if py3_exe.is_file():
        try:
            already_valid = filecmp.cmp(py_exe, py3_exe, shallow=False)
        except OSError:
            already_valid = False
        if already_valid:
            return 0
        stale = True

    # Honor CHECK_ONLY: report what would happen and return without
    if check_only:
        print(f"python3-exe-shim: check failed: {py3_exe} is stale or absent (would install)")
        return 1

    if stale:
        print(
            f"{_LOG_PREFIX} existing {_PYTHON3_EXE_NAME} diverges from {_PYTHON_EXE_NAME} "
            "(stale copy from prior patch?); re-shimming",
            file=sys.stderr,
        )
        try:
            py3_exe.unlink()
        except OSError:
            print(f"skip: _install_shim: py3_exe.unlink() failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass

    try:
        os.link(py_exe, py3_exe)
        print(f"{_LOG_PREFIX} hardlinked {py3_exe} -> {_PYTHON_EXE_NAME}", file=sys.stderr)
        return 0
    except OSError as link_exc:
        link_err = str(link_exc)

    try:
        shutil.copy2(py_exe, py3_exe)
        print(
            f"{_LOG_PREFIX} copied {_PYTHON_EXE_NAME} -> {py3_exe} (hardlink unavailable; "
            f"link error: {link_err})",
            file=sys.stderr,
        )
        return 0
    except OSError as copy_exc:
        print(f"{_LOG_PREFIX} ERROR: failed to install {_PYTHON3_EXE_NAME} shim at {py3_exe}", file=sys.stderr)
        print(f"{_LOG_PREFIX}   link error: {link_err}", file=sys.stderr)
        print(f"{_LOG_PREFIX}   copy error: {copy_exc}", file=sys.stderr)
        return 1


def _classify_python3() -> dict:
    resolved = shutil.which("python3")
    if not resolved:
        return {"classification": "not_found", "path": None}

    path = Path(resolved)
    if _is_appx_stub(path):
        return {"classification": "stub", "path": str(path)}

    try:
        proc = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            timeout=_APPX_STUB_PROBE_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"classification": "stub", "path": str(path)}

    if proc.returncode == 0:
        return {"classification": "ready", "path": str(path)}
    return {"classification": "stub", "path": str(path)}


@register_op("install.detect_python3_appx_stub")
async def _detect_python3_appx_stub(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "install.detect_python3_appx_stub" handler.

    Params: {} (none). Inspects the operator's own machine (PATH-resolved
    `python3`), not any repo state — `repo_root` is unused, matching the
    manifest's "none" scope verdict (own-machine probe, no
    `_origin_worktree` required; mirrors engine.drift / plugin_health.drift's
    own-machine-probe class).

    Returns: {"classification": "not_found"|"ready"|"stub", "path": str|None}
    — see `_classify_python3()`.
    """
    del params, repo_root
    return await asyncio.to_thread(_classify_python3)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. argv is accepted for CLI-shape parity but unused — the
    bash oracle took no positional args either (only the CHECK_ONLY env
    var)."""
    del argv
    if not _is_windows():
        return 0
    python_bin = _resolve_python_bin()
    check_only = bool(os.environ.get("CHECK_ONLY"))
    return _install_shim(python_bin, check_only)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
