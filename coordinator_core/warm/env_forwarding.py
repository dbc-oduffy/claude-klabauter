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

FOUR MODES, EXACTLY THE FOUR THAT EXIST TODAY.
  - ``borrow``   -- shape-gate-or-pop, mirrored into `os.environ` for the
                    block's duration under `isolated=True` only, restored
                    in a `finally`. Inherit-on-absent: a name the caller's
                    wire never carried keeps the server's own value. Right
                    only for a MACHINE-constant fact, where the server's
                    value and the caller's cannot differ.
  - ``caller``   -- as ``borrow``, except a name the caller's wire never
                    carried is POPPED, never inherited. For a PER-CALLER
                    fact: the server's own `os.environ` belongs to whichever
                    session spawned it, so inheriting hands every caller
                    that omits the name (every Bash-tool shell omits
                    `CLAUDE_PROJECT_DIR`) a stranger's value. Popping is
                    exactly what the cold path sees for such a caller.
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
from coordinator_core.session.mode_resolution import COORDINATOR_JOB_MODE

__all__ = ["Mode", "EnvEntry", "FORWARDING_SET", "CALLER_PREFIXES", "is_caller_prefixed", "generate_header"]

Mode = str
BORROW = "borrow"
CALLER = "caller"
REFUSE = "refuse"
OVERRIDE = "override"
_VALID_MODES = (BORROW, CALLER, REFUSE, OVERRIDE)


class EnvEntry(NamedTuple):

    name: str
    mode: Mode


def _entry(name: str, mode: Mode) -> EnvEntry:
    assert mode in _VALID_MODES, f"unknown env-forwarding mode: {mode!r}"
    return EnvEntry(name=name, mode=mode)


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
    _entry("CLAUDE_PROJECT_DIR", CALLER),
    _entry("CLAUDE_CODE_REMOTE", CALLER),
    # what the CALLER was invoked as -- and, same as `CLAUDE_CODE_REMOTE`
    _entry(COORDINATOR_JOB_MODE, CALLER),
    # COORDINATOR_AGENT_TYPE_HOST=coordinator; a warm-served CLI reads
    # CLAUDE_CODE_REMOTE and COORDINATOR_JOB_MODE above.
    _entry("COORDINATOR_AGENT_TYPE_HOST", CALLER),
)


#: (`warm.hook_http.FORWARDED_ENV_PREFIXES` is this tuple). No `FORWARDING_SET`
CALLER_PREFIXES: Tuple[str, ...] = (
    "COORDINATOR_ALLOW_",
    "COORDINATOR_OVERRIDE_",
    "COORDINATOR_PROBE_",
    "COORDINATOR_SCOPE_",
)


def is_caller_prefixed(name: str) -> bool:
    return any(len(name) > len(prefix) and name.startswith(prefix) for prefix in CALLER_PREFIXES)


_HEADER_BANNER = (
    "/* DO NOT EDIT — generated from coordinator_core/warm/env_forwarding.py\n"
    " * Regenerate via coordinator_core/warm/tests/test_env_forwarding_set.py\n"
    " * (or the same generator that test imports) whenever FORWARDING_SET\n"
    " * changes. A hand-edited copy of this file will be overwritten and\n"
    " * will fail the byte-pin test the moment it drifts. */\n"
)

_HEADER_GUARD = "COORDINATOR_WARM_DOOR_ENV_SET_H"


def _x_macro_body(names) -> list:
    body = [f"    X({name}) \\\n" for name in names]
    if body:
        body[-1] = body[-1].rstrip(" \\\n") + "\n"
    return body


def generate_header(
    entries: Tuple[EnvEntry, ...] = FORWARDING_SET,
    prefixes: Tuple[str, ...] = CALLER_PREFIXES,
) -> str:
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
    lines.extend(_x_macro_body(entry.name for entry in entries))
    lines.extend(
        [
            "\n",
            "/* Name PREFIXES forwarded from the caller's whole environment: every\n",
            " * variable whose name starts with one of these, non-empty, rides `_env`\n",
            " * under its own name. Per-session guard overrides, a namespace the guards\n",
            " * extend without telling the door. Expanded as a string literal per leg,\n",
            " * like DOOR_ENV_SET above. */\n",
            "#define DOOR_ENV_PREFIXES(X) \\\n",
        ]
    )
    lines.extend(_x_macro_body(prefixes))
    lines.append("\n")
    lines.append(f"#endif /* {_HEADER_GUARD} */\n")
    return "".join(lines)
