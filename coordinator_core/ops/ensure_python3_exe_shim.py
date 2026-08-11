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
ensure-python3-exe-shim.sh, example-doctrine-repo) bootstrapped a bare off-PATH
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
(example-doctrine-repo). The example-doctrine-repo .sh keeps its filename AND its bash-side security guard
(coordinator-trusted-root-guard.sh, a bash-only sourced lib out of this
port's scope) — it is now a thin veneer: bash runs the trust guard, then
invokes this module's main() in a child interpreter, rather than the
sh/python polyglot-exec trampoline shape used elsewhere. The polyglot shape
hands the WHOLE file to python on hand-off (python re-parses the file from
line 1), so bash-only statements between the shebang and the exec line would
break under python — incompatible with keeping the trust guard live. See the
.sh's own header comment for the mechanical detail.

Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

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

Spec backlink: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md
"""

from __future__ import annotations

import asyncio
import filecmp
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
    """OS gate. WSL reports the Linux platform value — correctly excluded,
    mirroring the bash oracle's uname -s case (MINGW*/MSYS*/CYGWIN*/Windows*
    only)."""
    return sys.platform.startswith("win")


def _resolve_python_bin() -> str:
    """Resolve the target interpreter to shim, in-process.

    See module docstring "Direct-import adaptation" for the PythonPinInvalid
    fallback rationale.
    """
    from coordinator_core.pyresolve import PythonPinInvalid, resolve_python_bin

    try:
        python_bin, _args = resolve_python_bin()
    except PythonPinInvalid:
        return sys.executable or ""
    return python_bin or ""


def _is_appx_stub(path: Path) -> bool:
    """True if `path` is a Windows Store App-Execution-Alias 0-byte
    reparse-point stub rather than a real file: the OS resolves it
    (`Path.exists()` true) but it isn't a regular file (`Path.is_file()`
    false). Off Windows this on-disk shape does not occur, so an ordinary
    file (or a nonexistent path) always evaluates False here — no
    Windows-only branch needed for correctness on macOS/Linux.

    Shared by `_install_shim()` (checked at the shim-install target) and
    `_classify_python3()` (checked at the PATH-resolved `python3` target) —
    same detection, two call sites, per DEC-7 / the op-classification
    manifest row's instruction to reuse rather than re-derive it.
    """
    return path.exists() and not path.is_file()


def _exe_basename_pth_trap(py_exe: Path, py3_exe: Path) -> Optional[Path]:
    """Detect the `._pth` executable-basename trap.

    `._pth` files are looked up by the *executable's own basename*, not by
    the DLL name. If `py_exe.parent` carries an executable-named
    `python._pth` (python.org's normal embeddable-adjacent shape keys
    isolation off `python.exe`'s own name), the freshly-hardlinked/copied
    `python3.exe` will look for a `python3._pth` that does not exist — and
    silently falls back to full registry + env + `site` path resolution
    instead of raising. Isolated mode turns off with no error.

    A DLL-named `pythonXX._pth` (e.g. `python312._pth`, the official
    embeddable-distribution shape) is unaffected — that is looked up by DLL
    name, so it applies identically to `python.exe` and `python3.exe`. Only
    the executable-basename form is a trap; this must not flag the DLL-named
    form.

    Detection is entirely a function of `py_exe` (its parent dir + stem
    build the `._pth` candidate); `py3_exe` is not inspected on disk here —
    it supplies only the shim's name for the operator-facing message text at
    the call site.

    Returns the offending path if the trap is present, else None.
    """
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

    # Skip if the resolver fell back to the py/pyw launcher — those are
    # registry-driven and don't have a stable on-disk directory to shim.
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

    # The resolver may have returned the windowless-preferred binary. For
    # the shim source we want the console binary — it lives next to the
    # windowless one in every python.org install.
    if not py_exe.is_file():
        print(
            f"{_LOG_PREFIX} {_PYTHON_EXE_NAME} not found at {py_exe} (PYTHON_BIN={python_bin}); skipping",
            file=sys.stderr,
        )
        return 0

    # Pre-shim state guards.
    #
    # (0) The `._pth` executable-basename trap. See _exe_basename_pth_trap()
    #     docstring: shimming here would silently produce a python3.exe that
    #     is NOT isolated the way python.exe is, with no error at all. Fail
    #     loud instead of installing a differently-behaving interpreter.
    #
    #     Review: code-reviewer (P2) — this preflight is deliberately ahead
    #     of the `already_valid` idempotency early-return below, not an
    #     oversight of where it belongs. A byte-identical existing shim is
    #     still wrongly isolated when the trap is present: the shim was
    #     created before the trap-file appeared (or before this guard
    #     existed), and a byte match only proves the shim mirrors
    #     python.exe's bytes, not that it mirrors python.exe's isolation
    #     behavior. Returning 0 on that byte match would restore exactly the
    #     silent isolation-off failure this guard exists to catch, so it
    #     must fire before, not after, the idempotency check.
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

    # (a) AppX zero-byte reparse-point stub at the shim path (left over from
    #     a Store Python install). Detect via _is_appx_stub() and direct the
    #     operator to install-substrate.sh's AppX stub cleanup before
    #     retrying.
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

    # (b) Stale copy after a patch upgrade. A hardlink auto-tracks the
    #     source binary's inode, so it stays correct. A copy-fallback shim
    #     does not — if the shim exists but its bytes differ from the
    #     source, remove and re-shim.
    stale = False
    if py3_exe.is_file():
        try:
            already_valid = filecmp.cmp(py_exe, py3_exe, shallow=False)
        except OSError:
            already_valid = False
        if already_valid:
            return 0  # already valid — idempotent, nothing to do
        stale = True

    # At this point the shim is absent or stale — a shim WILL be installed.
    # Honor CHECK_ONLY: report what would happen and return without
    # mutating.
    if check_only:
        print(f"python3-exe-shim: check failed: {py3_exe} is stale or absent (would install)")
        return 1

    # Real install path: remove stale copy (if any), then create shim below.
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

    # Hardlink equivalent of the Windows-native "link with hardlink" verb —
    # os.link on Windows calls the Win32 hardlink-creation API directly; no
    # elevation required on NTFS.
    try:
        os.link(py_exe, py3_exe)
        print(f"{_LOG_PREFIX} hardlinked {py3_exe} -> {_PYTHON_EXE_NAME}", file=sys.stderr)
        return 0
    except OSError as link_exc:
        link_err = str(link_exc)

    # Fallback: plain copy. Works across volumes and on non-NTFS (rare on
    # Windows but possible on FAT32 USB installs). Copies do NOT auto-track
    # the source binary's patch updates — the pre-shim diff guard above
    # re-shims on next run if drift is detected.
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
    """Three-way classify the PATH-resolved `python3`.

    Returns {"classification": "not_found"|"ready"|"stub", "path": str|None}.

    - "not_found": no `python3` resolves on PATH at all.
    - "stub": PATH resolves to the AppX 0-byte reparse-point stub
      (`_is_appx_stub()`), or the resolved path fails to run `--version`
      cleanly (nonzero exit, OSError, or timeout) — the AppX stub's
      characteristic failure-to-actually-execute-Python shape when the
      reparse point itself doesn't trip the exists()/is_file() split on a
      given filesystem view.
    - "ready": a real interpreter that answers `--version` with exit 0.

    No bash: PATH resolution is `shutil.which`, the version probe is a
    direct list-form `subprocess.run` (no shell=True).

    Deliberate isolation boundary — do not convert to an in-process import.
    Mechanism: distinct interpreter — classifies an unverified PATH hit
    (AppX stub detection); an in-process import cannot observe whether the
    resolved path actually executes Python. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
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
            # Review: code-reviewer (F2) — `resolved` is an unverified PATH hit
            # (could be an AppX stub or worse); guard against stdin-block hangs.
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
