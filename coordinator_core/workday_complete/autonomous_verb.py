
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

_REMAPPED_120_EXIT = 4

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SUBPROCESS_TIMEOUT = 20

_ENABLE_TOKENS = {"", "on", "yes"}
_DISABLE_TOKENS = {"off", "no", "stop"}


def parse_toggle(token: Optional[str]) -> str:
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
    return str(settings_home() / "bin" / "misc-session-and-guards")


def _run_sentinel_cli(args: List[str]) -> int:
    script = resolve_sentinel_cli()
    argv = [*resolve_launchable(script), *args]
    # CAPTURED, never inherited. This function runs both cold (this process's
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
        detail = stderr_text.strip() or "(child wrote nothing to stderr)"
        print(
            f"autonomous_verb: {' '.join(argv)} exited {result.returncode}: {detail}",
            file=sys.stderr,
        )
    return result.returncode


def enable(mode: str = "autonomous") -> int:
    return _run_sentinel_cli(["autonomous-sentinel", "enable", "--mode", mode])


def disable() -> int:
    return _run_sentinel_cli(["autonomous-sentinel", "disable"])


def main(argv: List[str]) -> int:
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
