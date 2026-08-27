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
from coordinator_core import launchable
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
    # The installed misc-session-and-guards is extensionless but is always a Python
    # script (produced from coordinator/bin/misc-session-and-guards.py). On Windows
    # keep resolve_launchable's .cmd-twin/shebang-sniffing behaviour (a bare path is
    # unexecutable there); on POSIX prefix sys.executable so the call doesn't depend
    # on the installed copy's own shebang + exec bit (which C4 strips upstream).
    if launchable._is_windows():
        argv = [*resolve_launchable(script), *args]
    else:
        argv = [sys.executable, script, *args]
    try:
        result = subprocess.run(
            argv,
            timeout=_SUBPROCESS_TIMEOUT,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"autonomous_verb: misc-session-and-guards invocation failed: {exc}", file=sys.stderr)
        return 3
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
        return rc

    rc = disable()
    if rc == 0:
        print("Autonomous mode disabled — context pressure hook will resume normal /handoff nudges.")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
