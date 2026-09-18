"""Shared machine-local registry WRITE seam for hook bodies under
`coordinator_core/hooks/`.

Ported from DoE-claude `coordinator/hooks/scripts/_registry_write.py` per
docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C3.

**This module never decides WHETHER to write** -- that judgement
(idempotence, never-overwrite, wrong-repo guards) belongs to each calling
hook, which has the context. This module only executes a write the caller
already decided on.

**Never a hand-edit of the registry TOML.** A concurrent session may be
writing it, and the machine-local `set` command is idempotent, atomic, and
concern-aware in ways a hand-rolled append is not -- see
`docs/wiki/machine-local-registry.md` § "Use this instead of editing
registry files by hand". This module runs that writer; it never composes
TOML.

Contract: `machine_local_set` returns `None` unconditionally and never
raises, never prints, never exits non-zero. Every failure mode --
unresolvable settings home, no implementation on either rung, a permissions
error, a spawn failure, a timeout -- degrades to a silent no-op. Its
callers are SessionStart hooks, where a raised exception greets every
session in the fleet with a stack trace.

ADAPTATION FROM THE PORTED SOURCE: DoE's copy resolved its own writer
(`<settings-home>/bin/_machine_local.py`, falling back to
`<plugin-root>/templates/bin/_machine_local.py`) via a private
`_engine_root`-resolved pair of helpers, because the doctrine plane and the
engine were separate repos and DoE had no native resolver of its own. This
engine already carries that identical two-rung resolution as
`coordinator_core.install._shared.resolve_machine_local_cli` (per that
module's own docstring: "resolved relative to THIS INSTALL/UNINSTALL RUN's
own coordinator plugin tree ... the caller passes `CLAUDE_PLUGIN_ROOT` or
an equivalent resolved root"), so this module reuses that resolver directly
rather than re-deriving it -- Engineering Defaults, "default to reusing,
not creating". `coordinator_core.install._shared.ml_set` is NOT reused
in turn: it prints a diagnostic on an unresolved CLI, which is the exact
"never prints" contract this SessionStart-facing seam must not carry (a
raised print greets every session with unwanted stderr noise on a box with
no settings home yet) -- the spawn itself is reimplemented here, silent by
construction, calling the SAME resolved argv `ml_set` would.

Multi-OS: invoked as `sys.executable <impl>.py …`, which is correct on
macOS, Windows and Linux alike -- never a shell, never a `.cmd`/`.ps1` leg,
never a bare-name PATH lookup. `no_console_creationflags()` suppresses the
Windows console flash under a headless SessionStart parent and degrades to
a harmless no-op elsewhere.
"""

from __future__ import annotations

import os
import subprocess

from coordinator_core.install._shared import resolve_machine_local_cli
from coordinator_core.win_portability import no_console_creationflags

#: Below the 15s whole-process timeout of the async SessionStart fan-in that
#: hosts this module's callers, so the in-process catch fires and returns on
#: the graceful path rather than losing the race to the harness hard-kill.
_SPAWN_TIMEOUT_SECONDS = 6


def machine_local_set(key: str, value: str, *, plugin_root: "str | None" = None) -> None:
    """Run `machine-local set <key> <value>`. Fail-open, silent, never raises.

    `plugin_root` is resolved by the caller (typically
    `os.environ.get("CLAUDE_PLUGIN_ROOT")`) and passed through to
    `resolve_machine_local_cli` unchanged; `None` falls back to that
    resolver's own PATH-only rung.
    """
    argv = resolve_machine_local_cli(plugin_root)
    if argv is None:
        return
    try:
        subprocess.run(
            argv + ["set", key, value],
            env=dict(os.environ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=_SPAWN_TIMEOUT_SECONDS,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return
