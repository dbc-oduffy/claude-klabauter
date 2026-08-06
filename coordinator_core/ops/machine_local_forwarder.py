"""
coordinator_core.ops.machine_local_forwarder — ported from
coordinator/bin/machine-local (DOE-PORT bin-entrypoint variant).

Purpose: bare-name forwarder for `machine-local`. Resolves and execs the real
reader with argv passed through, so bare `machine-local` resolves on POSIX tool
shells where the install dir is not on PATH. Delegates to
`coordinator_core.bare_forwarder.forward`, which already implements this exact
resolve-and-exec contract for other ported bare-name shims — and which resolves
the settings-home `bin` entry for this name FIRST, falling back to the legacy
`.claude/bin` entry under the home `_settings_home.home_dir()` reports (the
CLAUDE_HOME override when set, else `Path.home()`, which honours USERPROFILE on
Windows — never a bare HOME read, which has no Windows rung).

Behavior contract: exit 127 with a remediation message on stderr if the real
`machine-local` is not installed/executable at the resolved path; otherwise `os.execv`
replaces this process image with the real binary, so this module's own exit code is
whatever the real `machine-local` returns (never observed as a Python-level return —
`os.execv` does not return on success).

Example-doctrine-repo's swap story may retire the source `.sh` file entirely rather than keep it as a
polyglot trampoline over this module — this forwarder's own justification (bare-name
resolution on PATH-less POSIX shells) holds regardless of what, if anything, remains on
the example-doctrine-repo side.

Spec backlink: docs/plans/2026-06-18-machine-local-bare-invocation-macos.md
Prior bash implementation: coordinator/bin/machine-local.

Negative-spec (faithfully reproduced bash-oracle behavior — do NOT "fix" mid-port):
    - Does NOT search PATH. The bash oracle hardcoded
      `${CLAUDE_HOME:-$HOME}/.claude/bin/machine-local` as its sole target; this port
      resolves via `bare_forwarder.forward`, which is settings-home-first with that
      path as the fallback rung. The added rung is deliberate and is NOT a parity
      defect to "restore": `~/.claude/bin` was retired 2026-07-28, and a single-rung
      resolver against it exits 127, which callers reading non-zero as "key unset"
      report as an empty registry.
    - Does NOT fall back to a bundled/vendored copy if the real binary is missing —
      absence is a hard-fail (exit 127) with a remediation pointer, never a silent skip.
"""
from __future__ import annotations

import sys

from coordinator_core.bare_forwarder import forward


def main(argv: list[str]) -> int:
    """CLI entrypoint: delegates to bare_forwarder.forward("machine-local", argv).

    `forward` execs the resolved binary on success (never returns) or calls
    `sys.exit(127)` on failure (also never returns to this line) — the trailing
    `return 0` exists only to satisfy the `main(argv) -> int` entrypoint shape and is
    unreachable in practice.

    Review: code-reviewer — Finding 1 (2026-07-22 sidecar): `argv` was previously
    dead — `forward` read process-global `sys.argv` regardless of what was passed
    in here. Now threaded through explicitly so a caller-constructed `argv` can't
    silently diverge from what actually gets forwarded.
    """
    forward("machine-local", argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
