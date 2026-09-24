"""
coordinator_core.workday_complete.autonomous_verb — shared autonomous-sentinel
toggle verb, wrapping the existing `misc-session-and-guards autonomous-sentinel`
CLI (never reimplementing its enable/disable logic).

Purpose: `commands/autonomous.md`'s `/autonomous [on|off]` PM control and any
future workday-assembler step that needs to flip the autonomous-run sentinel
both resolve to this ONE verb — "shared assembler verb, two entry surfaces"
per the ceremony-complete computed-conversion survey. The on/off/yes/no/stop
token-to-action mapping that previously lived as prose inside autonomous.md's
`## Instructions` section is the deterministic branch this module owns; the
calling surface hands it a raw token (or none) and gets back an action, never
walks the branch itself.

Spec backlink: DoE-claude:pln-b1-ceremony-complete-computed--9ffa54 § C7

Negative-spec:
    - Does NOT reimplement sentinel enable/disable — every mutating call shells
      out to the landed `misc-session-and-guards autonomous-sentinel`
      subcommand (claude-klabauter `coordinator/bin/misc-session-and-guards.py`),
      which already owns session-id resolution and the fail-loud-on-empty-id
      contract. This module is argv-shape + dispatch only.
    - Does NOT touch `commands/autonomous.md` — this chunk's target is this
      file alone; the doctrine-surface fold (arg-parse removal, First-Officer
      posture block relocation to a wiki) is a separate surface edit.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.launchable import resolve_launchable

__all__ = [
    "ENABLE",
    "DISABLE",
    "parse_toggle",
    "resolve_sentinel_cli",
    "enable",
    "disable",
    "main",
]

ENABLE = "enable"
DISABLE = "disable"

#: CPython's own `Py_FinalizeEx() < 0` exit status (a stdout/stderr flush
#: failure at interpreter finalization) -- reserved by the interpreter, not
#: by this module. A failure here that happened to relay 120 verbatim was
#: indistinguishable from "this process itself hit that CPython condition",
#: so any exit this wrapper reports as 120 is remapped to this sentinel
#: instead (memo ask 1: "on any failure, exit non-120"). The stderr
#: diagnostic naming the real underlying code/detail is unaffected by the
#: remap -- only the numeric exit status changes.
_REMAPPED_120_EXIT = 4

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SUBPROCESS_TIMEOUT = 20

_ENABLE_TOKENS = {"", "on", "yes"}
_DISABLE_TOKENS = {"off", "no", "stop"}


def parse_toggle(token: Optional[str]) -> str:
    """Map a raw `/autonomous` argument token to `ENABLE` or `DISABLE`.

    Mirrors the mapping formerly documented as prose in autonomous.md's
    `## Instructions` section: empty/"on"/"yes" -> enable; "off"/"no"/"stop"
    -> disable. Case-insensitive, leading/trailing whitespace stripped.
    Raises `ValueError` on any other token — callers surface that as a usage
    error rather than silently guessing a direction.
    """
    normalized = (token or "").strip().lower()
    if normalized in _ENABLE_TOKENS:
        return ENABLE
    if normalized in _DISABLE_TOKENS:
        return DISABLE
    raise ValueError(
        f"autonomous_verb.parse_toggle: unrecognized token {token!r} — expected one of "
        f"empty/on/yes (enable) or off/no/stop (disable)"
    )


def resolve_sentinel_cli() -> str:
    """Resolve the installed `misc-session-and-guards` CLI path under settings-home."""
    return str(settings_home() / "bin" / "misc-session-and-guards")


def _run_sentinel_cli(args: List[str]) -> int:
    script = resolve_sentinel_cli()
    # "Extensionless under settings-home therefore Python source" stopped being
    # true on 2026-09-02, when the native-door cutover replaced every manifested
    # name in that directory with a compiled image at the bare name -- Mach-O on
    # POSIX, with no `.exe` to signal it. The former POSIX branch prefixed
    # sys.executable unconditionally on that premise and fed python a binary,
    # taking `/autonomous on|off` down on every cut-over box.
    # `resolve_launchable` already discriminates correctly: it prefixes an
    # interpreter for a non-executable `.py` only, and returns a bare path for a
    # native image, which is exactly why the other ~370 cut-over names survived.
    # Delegate on both platforms rather than carrying a second, drift-prone
    # answer to the same question.
    argv = [*resolve_launchable(script), *args]
    # CAPTURED, never inherited. This function runs both cold (this process's
    # own real stdout is the caller's) AND warm, in-process inside the server,
    # under `ops.invoke_from_argv._run_entrypoint`'s `contextlib.redirect_stdout`
    # -- which retargets the Python-level `sys.stdout` object, not this
    # process's OS-level fd 1/stderr. A bare `subprocess.run(argv, ...)` with no
    # stdout/stderr given inherits that untouched OS handle, so on the warm leg
    # the child's own stdout/stderr silently miss the redirect and land wherever
    # the warm server's real stdio was pointed at boot (typically DEVNULL) --
    # invisible to the actual caller on the other end of the pipe. Capturing
    # here and re-emitting through `print()` below routes the bytes through
    # whichever `sys.stdout`/`sys.stderr` is live in THIS call, cold or warm
    # alike, so the child's sentinel-path confirmation
    # (`misc-session-and-guards.py autonomous-sentinel enable`'s own
    # `print(str(_sentinel_path(...)))`) actually reaches the skill relaying it.
    try:
        result = subprocess.run(
            argv,
            timeout=_SUBPROCESS_TIMEOUT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"autonomous_verb: misc-session-and-guards invocation failed: {exc}", file=sys.stderr)
        return 3
    stdout_text = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
    stderr_text = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
    if stdout_text:
        print(stdout_text, end="" if stdout_text.endswith("\n") else "\n")
    if result.returncode != 0:
        # Fail LOUD (memo ask 1): a bare relayed exit code with nothing on
        # stderr is indistinguishable from a hang or a silently-swallowed
        # crash -- exactly what let a child's CPython exit 120 (stdout-flush
        # failure at interpreter finalization, `Py_FinalizeEx` < 0) pass
        # through this wrapper with no diagnostic at all. Every nonzero exit
        # now names the underlying command, its own exit code, and relays
        # whatever the child DID manage to write to stderr.
        detail = stderr_text.strip() or "(child wrote nothing to stderr)"
        print(
            f"autonomous_verb: {' '.join(argv)} exited {result.returncode}: {detail}",
            file=sys.stderr,
        )
    return result.returncode


def enable(mode: str = "autonomous") -> int:
    """Enable the autonomous-run sentinel via the misc-session-and-guards CLI.

    `mode` is forwarded verbatim to `autonomous-sentinel enable --mode`
    (`autonomous` for `/autonomous`'s own callers, `mise-en-place` for
    `/mise-en-place`'s co-writer — see that command's own sentinel write).
    """
    return _run_sentinel_cli(["autonomous-sentinel", "enable", "--mode", mode])


def disable() -> int:
    """Disable (remove) the autonomous-run sentinel via the misc-session-and-guards CLI."""
    return _run_sentinel_cli(["autonomous-sentinel", "disable"])


def main(argv: List[str]) -> int:
    """CLI entrypoint: `autonomous_verb.py [on|off|yes|no|stop]`.

    Resolves the toggle direction from `argv[0]` (absent -> enable, per
    `parse_toggle`), invokes the wrapped sentinel CLI, and prints the same
    PM-facing confirmation lines `/autonomous` has always surfaced.
    """
    token = argv[0] if argv else None
    try:
        action = parse_toggle(token)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if action == ENABLE:
        rc = enable(mode="autonomous")
        if rc == 0:
            print(
                "Autonomous mode enabled — context pressure hook will emit "
                "informational-only messages (no /handoff nudge). Use "
                "`/autonomous off` to restore normal behavior."
            )
        return _reported_exit(rc)

    rc = disable()
    if rc == 0:
        print("Autonomous mode disabled — context pressure hook will resume normal /handoff nudges.")
    return _reported_exit(rc)


def _reported_exit(rc: int) -> int:
    """Remap `rc` for this process's own exit status (see `_REMAPPED_120_EXIT`).

    `_run_sentinel_cli` has already printed the stderr diagnostic naming the
    real underlying code by the time this runs; this only prevents THIS
    wrapper's own exit status from colliding with CPython's reserved 120."""
    return _REMAPPED_120_EXIT if rc == 120 else rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
