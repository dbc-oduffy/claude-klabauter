"""
coordinator_core.ops.cmd_autorun_guard — closes the cmd.exe coverage gap in
coordinator's `claude` interception.

Problem: coordinator guards reach a session only via `claude-doe`
(coordinator/bin/claude-doe.py[.cmd]), which execs `claude --plugin-dir
<DoE>/coordinator`. Interception of a bare `claude` invocation happens via
shell FUNCTION shims (pwsh profile, Windows PowerShell 5.1 profile, bash rc —
see `coordinator_core.install.shell_rc_guard`). None of those load in
cmd.exe, which has no profile/rc concept at all — a bare `claude` typed in
cmd.exe resolves straight to the real `claude` executable on PATH, with zero
coordinator guards. `-NoProfile`/non-interactive pwsh sessions have the same
hole for the same reason (the profile that carries the shim is never
sourced) — see "What this does NOT close" below; that leg stays open.

Mechanism: cmd.exe's `AutoRun` registry value
(`HKCU\\Software\\Microsoft\\Command Processor\\AutoRun`) is a command line
cmd.exe runs on EVERY interactive startup, unconditionally — not a PATH
lookup, so unlike the declined python3.exe launcher-shim PE (see DR-284),
this mechanism's efficacy is NOT a function of the host's PATH order. This
module writes a `doskey claude=claude-doe $*` macro into that AutoRun value
(HKCU only — never HKLM, which would require elevation and is a materially
more invasive, machine-wide change out of this module's scope), guarded so
it never clobbers an operator's own AutoRun content.

Spec backlink: docs/decisions/DR-284-decline-the-launcher-shim-pe-build-for-d.md
(this module's DOSKEY_AUTORUN mechanism is a DIFFERENT shape from what that
record declined — see the superseding decision record this module ships
with, docs/decisions/DR-285-*.md, for the full comparison).

§ Registry access seam
Registry reads/writes are isolated behind three module-level functions
(`_read_autorun`, `_write_autorun`, `_delete_autorun`) so tests can
monkeypatch them directly and never touch a real HKCU hive, on any
platform — mirrors this repo's own registry-touching op
(`coordinator_core.ops.detect_hardware`) in spirit, but this module's own
seam, not a shared import (that op's registry surface is unrelated —
hardware enumeration, not AutoRun).

§ Idempotency / clobber-avoidance
`_desired_macro` (`"doskey claude=claude-doe $*"`) is treated as an
identifiable, self-marking substring of the AutoRun value — not wrapped in a
sentinel comment pair, because cmd.exe's `rem` consumes the REST of the
line (including any `&`-chained commands after it), so a sentinel-comment
scheme (as `coordinator_core.install.shell_rc_guard` uses for POSIX rc
files) cannot coexist with further `&`-chained content on the same AutoRun
line. The exact macro text IS the marker:
  - absent from an empty/unset AutoRun -> the write SETS AutoRun to exactly
    the macro text.
  - absent from a non-empty AutoRun -> the write APPENDS `" & " + macro` to
    the operator's existing content, never overwriting it.
  - already present (anywhere in the string) -> no-op.
Strip (uninstall) reverses this exactly: removes the macro text plus one
adjacent `" & "` (either side), never touching any other content. If the
macro text IS the entire value, the AutoRun value itself is deleted (not
left as an empty string) — an empty AutoRun value still runs (as a no-op)
on every cmd.exe start, which is observably different from no AutoRun at
all only in that it costs a wasted CreateProcess/registry read; deleting it
restores the pre-install state exactly.

§ What this does NOT close
`-NoProfile`/non-interactive PowerShell sessions load no profile, so a
profile-installed shell-function shim never has a chance to intercept
`claude` there — and AutoRun is a cmd.exe-only mechanism (PowerShell has no
AutoRun equivalent), so THIS module does not close that gap either. Stated
here, not silently left uncovered: an `-NoProfile` invocation of `claude`
remains ungated by design of this change, and closing it (if ever
attempted) needs a materially different mechanism (e.g. a machine-wide
policy/wrapper on the `claude` binary itself), out of this module's scope.

§ Hard machine-mutation gate
Every mutating call (`write_cmd_autorun_guard`, `strip_cmd_autorun_guard`)
is gated behind `coordinator_core.install.substrate._refuse_machine_mutation`
— the SAME belt-and-braces `COORDINATOR_DISABLE_MACHINE_MUTATION` gate every
other registry/rc-file writer in this tree honours (see
`coordinator_core.install.shell_rc_guard._write_block_to_file` for the
sibling pattern this module follows). Deferred import (not module-level) —
mirrors `shell_rc_guard.py`'s own documented reason: avoiding an import-time
cycle through `coordinator_core.install.substrate`.

Negative-spec:
  - Never writes to HKLM. HKLM AutoRun is machine-wide, requires elevation,
    and is a materially different (and more invasive) mutation than this
    module's HKCU-only scope covers — out of scope, not merely undone.
  - Never touches PowerShell profiles, PATH, or any file outside the
    registry — `coordinator_core.install.shell_rc_guard` already owns the
    POSIX/pwsh-profile shim surface; this module is cmd.exe-only.
  - Never mutates on a non-Windows platform (`_is_windows()` gate, mirrors
    `coordinator_core.ops.ensure_python3_exe_shim`'s own OS gate) — reads
    degrade to `{"classification": "not_windows", ...}` rather than raising.
  - Never mutates when `check_only`/`COORDINATOR_DISABLE_MACHINE_MUTATION`
    is set — reports what would happen and returns.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Optional

from coordinator_core.ipc import register_op

_AUTORUN_KEY = r"Software\Microsoft\Command Processor"
_AUTORUN_VALUE_NAME = "AutoRun"
_DESIRED_MACRO = "doskey claude=claude-doe $*"
_JOIN = " & "
_LOG_PREFIX = "[cmd-autorun-guard]"


def _is_windows() -> bool:
    """OS gate — mirrors `coordinator_core.ops.ensure_python3_exe_shim._is_windows`.
    WSL reports the Linux platform value and is correctly excluded (WSL has
    no cmd.exe / no Win32 registry of its own)."""
    return sys.platform.startswith("win")


def _read_autorun() -> Optional[str]:
    """Read `HKCU\\Software\\Microsoft\\Command Processor\\AutoRun`.

    Returns the current string value, or ``None`` if the value (or the key)
    does not exist. Never raises on absence — only a genuine unexpected
    registry error propagates. Isolated as its own function (rather than
    inlined) so tests can monkeypatch this directly and never touch a real
    HKCU hive, on any platform (including non-Windows, where `winreg` does
    not exist at all)."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTORUN_KEY) as key:
            value, _type = winreg.QueryValueEx(key, _AUTORUN_VALUE_NAME)
            return value
    except FileNotFoundError:
        return None


def _write_autorun(value: str) -> None:
    """Set `HKCU\\Software\\Microsoft\\Command Processor\\AutoRun` to
    `value` (creating the key if absent — `Command Processor` under HKCU
    always exists in practice on a real Windows profile, but
    `CreateKeyEx` degrades gracefully either way). REG_SZ, matching the
    type Windows itself uses for this value."""
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _AUTORUN_KEY) as key:
        winreg.SetValueEx(key, _AUTORUN_VALUE_NAME, 0, winreg.REG_SZ, value)


def _delete_autorun() -> None:
    """Delete the `AutoRun` value entirely (not merely set it to ``""``) —
    see module docstring § Idempotency for why an empty-string value is not
    equivalent to no value at all. No-op (does not raise) if already
    absent."""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _AUTORUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, _AUTORUN_VALUE_NAME)
    except FileNotFoundError:
        pass


def _macro_present_as_token(current: str) -> bool:
    """True only when `_DESIRED_MACRO` appears as a `_JOIN`-delimited
    command token — the exact inverse of how `write_cmd_autorun_guard`
    composes the value (macro alone, or appended via `_JOIN`). A raw
    substring test would misclassify a foreign AutoRun value that merely
    contains the macro text without the delimiter (e.g. inside a `rem`
    comment, or concatenated without `" & "` by some other tool) as
    "covered" — see module docstring § Idempotency.

    Review: coordinator:code-reviewer — token-boundary anchoring, matches
    the composition/decomposition symmetry `write`/`strip` already rely on.
    """
    return (
        current == _DESIRED_MACRO
        or current.startswith(_DESIRED_MACRO + _JOIN)
        or current.endswith(_JOIN + _DESIRED_MACRO)
        or (_JOIN + _DESIRED_MACRO + _JOIN) in current
    )


def _classify(current: Optional[str]) -> str:
    """Three-way classification of the current AutoRun value against our
    macro — see module docstring § Idempotency.

    - "uncovered": AutoRun unset or empty.
    - "covered": our macro text is present as a `_JOIN`-delimited token
      (the exact shape `write_cmd_autorun_guard` composes).
    - "foreign_present": AutoRun is set to something that does not contain
      our macro as a delimited token (an operator's own AutoRun content,
      even if it happens to embed the macro text as a substring).
    """
    if not current:
        return "uncovered"
    if _macro_present_as_token(current):
        return "covered"
    return "foreign_present"


def detect_cmd_autorun_coverage() -> dict:
    """Read-only probe — no mutation, no consent gating needed (mirrors
    `ensure_python3_exe_shim`'s own read-only `_classify_python3`, DEC-7
    idempotency note: identical PATH/registry state always yields the same
    classification).

    Returns ``{"classification": "not_windows"|"uncovered"|"covered"|
    "foreign_present", "current_value": str|None}``.
    """
    if not _is_windows():
        return {"classification": "not_windows", "current_value": None}
    current = _read_autorun()
    return {"classification": _classify(current), "current_value": current}


def _refuse_if_disabled(what: str) -> Optional[str]:
    """Deferred import of the shared belt-and-braces gate — see module
    docstring § Hard machine-mutation gate for why this is deferred rather
    than a module-level import (mirrors `shell_rc_guard.py`'s own documented
    reason: avoiding an import-time cycle through
    `coordinator_core.install.substrate`)."""
    from coordinator_core.install import substrate as _substrate_mod

    return _substrate_mod._refuse_machine_mutation(
        _AUTORUN_VALUE_NAME, what=what, check_temp_path=False
    )


def write_cmd_autorun_guard(check_only: bool = False) -> dict:
    """Apply the AutoRun guard (see module docstring § Idempotency).

    Returns ``{"classification": <pre-write classification>, "modified":
    bool, "would_modify": bool}``. Never mutates when not on Windows, when
    already "covered", when `check_only` is set, or when
    `COORDINATOR_DISABLE_MACHINE_MUTATION` (via `_refuse_machine_mutation`)
    refuses.
    """
    if not _is_windows():
        print(f"{_LOG_PREFIX} not running on Windows — no-op", file=sys.stderr)
        return {"classification": "not_windows", "modified": False, "would_modify": False}

    current = _read_autorun()
    classification = _classify(current)

    if classification == "covered":
        return {"classification": classification, "modified": False, "would_modify": False}

    desired = _DESIRED_MACRO if not current else f"{current}{_JOIN}{_DESIRED_MACRO}"

    if check_only:
        print(
            f"{_LOG_PREFIX} would set AutoRun to: {desired!r} (currently: {current!r})",
            file=sys.stderr,
        )
        return {"classification": classification, "modified": False, "would_modify": True}

    blocked = _refuse_if_disabled(f"write HKCU AutoRun={desired!r}")
    if blocked:
        print(f"{_LOG_PREFIX} REFUSED: {blocked}", file=sys.stderr)
        return {"classification": classification, "modified": False, "would_modify": True}

    _write_autorun(desired)
    print(f"{_LOG_PREFIX} AutoRun {'appended' if current else 'set'} — now: {desired!r}", file=sys.stderr)
    return {"classification": classification, "modified": True, "would_modify": False}


def strip_cmd_autorun_guard(check_only: bool = False) -> dict:
    """Reverse `write_cmd_autorun_guard` — the uninstall leg for this
    surface. Removes only our macro text (plus one adjacent `" & "`, either
    side); any other AutoRun content the operator or another tool wrote is
    left untouched. Deletes the value outright if our macro was the entire
    content (see module docstring § Idempotency).

    Returns ``{"classification": <pre-strip classification>, "modified":
    bool, "would_modify": bool}``.
    """
    if not _is_windows():
        return {"classification": "not_windows", "modified": False, "would_modify": False}

    current = _read_autorun()
    classification = _classify(current)

    if classification in ("uncovered", "not_windows", "foreign_present"):
        # "foreign_present" means AutoRun is set but does not contain our
        # macro — nothing of ours to strip, and rewriting it (even to the
        # same bytes) would be a needless registry write plus a dishonest
        # "modified": True. Preserve third-party AutoRun content untouched.
        return {"classification": classification, "modified": False, "would_modify": False}

    assert current is not None  # classification == "covered" implies current is truthy

    remainder = current.replace(f"{_JOIN}{_DESIRED_MACRO}", "")
    remainder = remainder.replace(f"{_DESIRED_MACRO}{_JOIN}", "")
    remainder = remainder.replace(_DESIRED_MACRO, "") if remainder == current else remainder

    if check_only:
        action = "delete AutoRun value" if not remainder else f"set AutoRun to: {remainder!r}"
        print(f"{_LOG_PREFIX} would {action} (currently: {current!r})", file=sys.stderr)
        return {"classification": classification, "modified": False, "would_modify": True}

    blocked = _refuse_if_disabled(
        "delete HKCU AutoRun" if not remainder else f"set HKCU AutoRun={remainder!r}"
    )
    if blocked:
        print(f"{_LOG_PREFIX} REFUSED: {blocked}", file=sys.stderr)
        return {"classification": classification, "modified": False, "would_modify": True}

    if remainder:
        _write_autorun(remainder)
        print(f"{_LOG_PREFIX} AutoRun stripped — now: {remainder!r}", file=sys.stderr)
    else:
        _delete_autorun()
        print(f"{_LOG_PREFIX} AutoRun value deleted (macro was the entire content)", file=sys.stderr)

    return {"classification": classification, "modified": True, "would_modify": False}


@register_op("install.detect_cmd_autorun_coverage")
async def _detect_handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'install.detect_cmd_autorun_coverage' handler. Params: {}
    (none). Own-machine probe, no repo scope — matches
    `install.detect_python3_appx_stub`'s own "none" scope verdict."""
    del params, repo_root
    return await asyncio.to_thread(detect_cmd_autorun_coverage)


@register_op("install.write_cmd_autorun_guard")
async def _write_handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'install.write_cmd_autorun_guard' handler.

    Params: ``{check_only: bool}`` (default False). Writes to the
    operator's own HKCU hive, not the caller's repo — `repo_root` unused,
    same scope verdict as `install.write_shell_rc_guard_block`."""
    del repo_root
    check_only = bool(params.get("check_only", False))
    return await asyncio.to_thread(write_cmd_autorun_guard, check_only)


@register_op("install.strip_cmd_autorun_guard")
async def _strip_handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'install.strip_cmd_autorun_guard' handler — the uninstall
    leg. Params: ``{check_only: bool}`` (default False)."""
    del repo_root
    check_only = bool(params.get("check_only", False))
    return await asyncio.to_thread(strip_cmd_autorun_guard, check_only)


def main(argv: Optional[list] = None) -> int:
    """CLI entry point.

    ``argv[0]`` (if given) is one of ``detect`` | ``apply`` | ``strip``
    (default ``detect``). ``--check-only`` is honoured by ``apply``/
    ``strip``. Mirrors `ensure_python3_exe_shim.main`'s CHECK_ONLY-env
    convention but as an explicit flag, since this CLI has three verbs
    rather than one."""
    args = list(argv) if argv is not None else sys.argv[1:]
    check_only = "--check-only" in args
    verbs = [a for a in args if a != "--check-only"]
    verb = verbs[0] if verbs else "detect"

    if verb == "detect":
        result = detect_cmd_autorun_coverage()
    elif verb == "apply":
        result = write_cmd_autorun_guard(check_only=check_only)
    elif verb == "strip":
        result = strip_cmd_autorun_guard(check_only=check_only)
    else:
        print(f"{_LOG_PREFIX} unrecognized verb {verb!r} (expected detect|apply|strip)", file=sys.stderr)
        return 2

    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
