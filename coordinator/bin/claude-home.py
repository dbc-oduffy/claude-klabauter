"""claude-home.py — door-eligible engine entrypoint for `claude-home`.

Spec backlink: docs/plans/2026-08-30-twenty-one-bin-names-reach-the-door-or-
are-thoroughly-dead.md C5 (Windows leg cutover).

Purpose: the door (`_resolve_entrypoint_script`, `door.c :: fall_through`,
`door_posix.c`) resolves exactly `{engine_root}/coordinator/bin/<name>.py`,
falling back to the extensionless `{engine_root}/coordinator/bin/<name>`.
Neither existed for `claude-home` before this file, which is why the name
could never reach the door (see the plan body's "Established facts"). This
file is that missing entrypoint — a thin trampoline, carrying no logic of
its own, into the real implementation at
`coordinator/lib/claude-home/_claude_home.py`.

WINDOWS-ONLY CUTOVER, BY DESIGN — NOT AN OVERSIGHT. Creating this file makes
`claude-home` door-eligible, but eligibility alone is not cutover: the
generic agent-helper derivation (`substrate._derive_agent_helper_target_map`)
still excludes `claude-home` on POSIX via `_AGENT_HELPER_RESERVED_NAMES`
(OS-gated, see that constant's own comment), so only the Windows leg is
actually cut over to the native door image. On POSIX, `named_forwarder_path`
places a native door image AT the bare `<name>` path — the exact path the
`ch_family`-installed extensionless `claude-home` POSIX shim already
occupies — and `door_posix.c :: fall_through`'s rewritten two-candidate
resolution has never been compiled (open P2 row
`state/bug-backlog/2026-08-30-door-posix-c-s-rewritten-fall-through-sh-152f899034f4.yaml`).
Cutting POSIX over today would replace a working shim with an unbuilt/
unverified door leg. Deferred explicitly, not by omission.

Usage: identical to the co-located POSIX `claude-home` launcher and the
`_claude_home.py` implementation's own `_main(argv)` dispatch — see
`coordinator/lib/claude-home/_claude_home.py`'s module docstring for the
full subcommand list.
"""
from __future__ import annotations

import sys
from pathlib import Path

_IMPL_DIR = Path(__file__).resolve().parents[1] / "lib" / "claude-home"

if str(_IMPL_DIR) not in sys.path:
    sys.path.insert(0, str(_IMPL_DIR))

from _claude_home import _main  # noqa: E402

if __name__ == "__main__":
    sys.exit(_main(sys.argv))
