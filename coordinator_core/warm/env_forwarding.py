"""env_forwarding -- SSOT for the environment names the warm door forwards
from the CALLING process to a warm-served op.

WHY THIS MODULE EXISTS. Before this row, the set of names crossing the
door boundary was three hand-written blocks: `door.c`'s wide-string reads,
`door_posix.c`'s narrow-string reads, and `entry_seam.py`'s Python-side
`borrow`/`refuse`/`override` handling -- three lists that happened to agree
because nobody had added a fourth name yet. This module declares the set
ONCE, as typed data, and `door_env_set.h` (generated -- see
`coordinator_core/warm/tests/test_env_forwarding_set.py`) is the artifact
that makes drift between the two C legs and this module structurally
impossible rather than merely documented.

THREE MODES, EXACTLY THE THREE THAT EXIST TODAY.
  - ``borrow``   -- shape-gate-or-pop, mirrored into `os.environ` for the
                    block's duration under `isolated=True` only, restored
                    in a `finally`. The ordinary mode.
  - ``refuse``   -- a mismatch against the server's own resolved value
                    returns a refusal PRE-DISPATCH. `COORDINATOR_SETTINGS_
                    HOME` only; see `warm.server._settings_home_refusal`
                    and `warm.settings_home_claim.mismatch_message` for the
                    refusal text and trigger condition this module does not
                    re-derive.
  - ``override`` -- bound through `session.core.session_identity_override`
                    with its existing UUID shape gate, top-tier name bound
                    and lower-tier names popped. The `session.core.
                    SESSION_ENV_PRECEDENCE` triple only.

WHAT THIS MODULE DOES NOT CARRY. `mode` is not emitted into
`door_env_set.h`: neither C leg reads it, mode dispatch happens Python-side
at the one seam in `warm.entry_seam` / `warm.server`, and DR-404's negative
spec is satisfied there by three explicitly-named branches, not by mode
travelling as C-facing data. A per-entry shape-gate field is likewise not
carried: the `override` entry's UUID shape gate stays where it already
lives, inside `session_identity_override`; the `refuse` entry and the
`borrow` entries are validated by their own read logic (a
length-probe-then-read that already rejects a malformed value), not by a
declared gate axis. `CLAUDE_PID` is deliberately NOT an entry here -- it
stays derived from `GetCurrentProcessId()`/`getpid()`, never read from the
environment.

SEED SET (C1's judgment call, deliberately narrow): exactly what was on
the wire at C1 (`COORDINATOR_SETTINGS_HOME` plus the `SESSION_ENV_
PRECEDENCE` triple) plus `MACHINE_LOCAL_REGISTRY_DIR`.

WIDENED SET (C7): seven more `borrow` names -- `CLAUDE_HOME`,
`CLAUDE_PLUGIN_ROOT`, `CLAUDE_CONFIG_DIR`, `MACHINE_LOCAL_IMPL`,
`COORDINATOR_ROOT`, `DOE_ROOT`, `CLAUDE_PROJECT_DIR` -- the remaining
census names that are path-valued caller-owned facts read the same way
`MACHINE_LOCAL_REGISTRY_DIR` already is. The OS-level census names
(`HOME`, `USERPROFILE`, `PATH`, `LOCALAPPDATA`, `TMPDIR`, `SYSTEMROOT`)
stay OFF: identical across callers on one box, never part of this
defect. `CLAUDE_PID` stays OFF: derived from
`GetCurrentProcessId()`/`getpid()`, never read from the environment.

Spec backlink: docs/plans/2026-09-01-the-warm-door-forwards-a-declared-env-set.md
chunks C1, C7.
"""
from __future__ import annotations

from typing import NamedTuple, Tuple

from coordinator_core.session.core import SESSION_ENV_PRECEDENCE

__all__ = ["Mode", "EnvEntry", "FORWARDING_SET", "generate_header"]

#: The three modes that exist today -- see module docstring. A fourth mode
#: is a new row's judgment call, not a value to add here casually.
Mode = str
BORROW = "borrow"
REFUSE = "refuse"
OVERRIDE = "override"
_VALID_MODES = (BORROW, REFUSE, OVERRIDE)


class EnvEntry(NamedTuple):
    """One declared forwarding-set member: an env var name and its mode.

    Never a bare name -- every entry on the wire has exactly one of the
    three modes documented at module level, and that mode is what a
    consumer (Python-side only; see module docstring for why the C legs do
    not read it) branches on.
    """

    name: str
    mode: Mode


def _entry(name: str, mode: Mode) -> EnvEntry:
    assert mode in _VALID_MODES, f"unknown env-forwarding mode: {mode!r}"
    return EnvEntry(name=name, mode=mode)


#: The declared forwarding set -- SSOT. Ordered: the `refuse` entry first
#: (pre-dispatch, so it is checked before any `borrow`/`override` entry is
#: even relevant), then the `override` triple in its existing precedence
#: order, then `borrow` entries.
FORWARDING_SET: Tuple[EnvEntry, ...] = (
    _entry("COORDINATOR_SETTINGS_HOME", REFUSE),
    *(_entry(name, OVERRIDE) for name in SESSION_ENV_PRECEDENCE),
    _entry("MACHINE_LOCAL_REGISTRY_DIR", BORROW),
    _entry("CLAUDE_HOME", BORROW),
    _entry("CLAUDE_PLUGIN_ROOT", BORROW),
    _entry("CLAUDE_CONFIG_DIR", BORROW),
    _entry("MACHINE_LOCAL_IMPL", BORROW),
    _entry("COORDINATOR_ROOT", BORROW),
    _entry("DOE_ROOT", BORROW),
    _entry("CLAUDE_PROJECT_DIR", BORROW),
)


_HEADER_BANNER = (
    "/* DO NOT EDIT — generated from coordinator_core/warm/env_forwarding.py\n"
    " * Regenerate via coordinator_core/warm/tests/test_env_forwarding_set.py\n"
    " * (or the same generator that test imports) whenever FORWARDING_SET\n"
    " * changes. A hand-edited copy of this file will be overwritten and\n"
    " * will fail the byte-pin test the moment it drifts. */\n"
)

#: Emitted header shape, X-macro list, NAMES ONLY (see module docstring:
#: `mode` is Python-side-only data, never C-facing). Each leg `#define`s
#: its own `X` locally and `#undef`s it after -- see `door_env_set.h`'s own
#: comment and door.c/door_posix.c for the two expansions.
_HEADER_GUARD = "COORDINATOR_WARM_DOOR_ENV_SET_H"


def generate_header(entries: Tuple[EnvEntry, ...] = FORWARDING_SET) -> str:
    """Render `door_env_set.h`'s exact committed bytes from `entries`.

    Pure function of `FORWARDING_SET` (or an explicit override, used only
    by tests) -- no filesystem I/O here. `test_env_forwarding_set.py`
    calls this and compares the result byte-for-byte against the committed
    `door_env_set.h`, the same "regenerate in-memory, diff against
    committed bytes" shape `contract.cockpit_schema.emit_schema`'s pin test
    already uses in this repo.
    """
    lines = [
        _HEADER_BANNER,
        f"#ifndef {_HEADER_GUARD}\n",
        f"#define {_HEADER_GUARD}\n",
        "\n",
        "/* X-macro list of forwarded env-var names. Each consumer defines\n",
        " * its own X(name) before including this file and #undefs it after:\n",
        " *\n",
        " *   #define X(name) L\"\" #name,\n",
        " *   static const wchar_t *const kDoorEnvSet[] = { DOOR_ENV_SET(X) };\n",
        " *   #undef X\n",
        " *\n",
        " * door_posix.c's X expands to a plain char* literal instead. See\n",
        " * those files for the actual expansion each leg uses. */\n",
        "#define DOOR_ENV_SET(X) \\\n",
    ]
    entry_lines = [f"    X({entry.name}) \\\n" for entry in entries]
    if entry_lines:
        entry_lines[-1] = entry_lines[-1].rstrip(" \\\n") + "\n"
    lines.extend(entry_lines)
    lines.append("\n")
    lines.append(f"#endif /* {_HEADER_GUARD} */\n")
    return "".join(lines)
